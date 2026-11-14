import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, cast

import typer

from review_watcher import launchd
from review_watcher.auth import AuthChecker, sign_in, trace_to_stderr
from review_watcher.config import LoadedConfig, load_config
from review_watcher.context import SOURCE_TOOLS, CliContextFetcher
from review_watcher.feedback import FeedbackSummary, FeedbackWatcher
from review_watcher.github import GhCliGitHub
from review_watcher.launcher import CommandLauncher
from review_watcher.lock import RunLockHeld, run_lock
from review_watcher.logging_setup import configure_logging
from review_watcher.models import PullRequestRef
from review_watcher.notifier import (
    Notification,
    Notifier,
    OsascriptNotifier,
    TerminalNotifierNotifier,
    notify_logging_failures,
    resolve_binary,
)
from review_watcher.pipeline import ERROR_TITLE, Pipeline, RunOutcome, RunSummary
from review_watcher.proc import base_env, run_command
from review_watcher.repo_cache import RepoCache
from review_watcher.reviewer import (
    ClaudeCliReviewer,
    NullReviewer,
    Reviewer,
    resolve_claude_binary,
    verify_claude_version,
)
from review_watcher.state import StateStore, utc_now

PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?")
STATE_RETENTION = timedelta(days=90)
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, help="Review GitHub PRs where your review is requested.")


@dataclass(frozen=True)
class Options:
    config_path: Path
    dry_run: bool
    no_llm: bool
    verbose: bool


@dataclass(frozen=True)
class Runtime:
    loaded: LoadedConfig
    pipeline: Pipeline
    feedback: FeedbackWatcher
    github: GhCliGitHub
    notifier: Notifier
    state: StateStore
    repos: RepoCache


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option("--config", help="Path to config.toml.")] = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Review but post nothing to GitHub.")
    ] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Skip the claude review step.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Debug logging.")] = False,
) -> None:
    ctx.obj = Options(config_path=config, dry_run=dry_run, no_llm=no_llm, verbose=verbose)


def main() -> None:
    app()


def _options(ctx: typer.Context) -> Options:
    return cast("Options", ctx.obj)


def _load(opts: Options) -> LoadedConfig:
    loaded = load_config(opts.config_path)
    configure_logging(loaded.var_dir / "logs", verbose=opts.verbose)
    return loaded


def _repo_cache(loaded: LoadedConfig, git_binary: Path) -> RepoCache:
    return RepoCache(
        git_binary=git_binary,
        bare_root=loaded.var_dir / "repos",
        worktree_root=loaded.var_dir / "worktrees",
        related_root=loaded.var_dir / "related",
    )


async def _build_runtime(opts: Options, loaded: LoadedConfig) -> Runtime:
    settings = loaded.settings
    git_binary = resolve_binary("git")
    gh_binary = resolve_binary("gh")
    reviewer: Reviewer
    if opts.no_llm:
        reviewer = NullReviewer()
    else:
        claude_binary = resolve_claude_binary()
        await verify_claude_version(claude_binary)
        reviewer = ClaudeCliReviewer(binary=claude_binary, config=settings.reviewer)

    notifier: Notifier
    if settings.notifier.backend == "terminal_notifier":
        notifier = TerminalNotifierNotifier(resolve_binary("terminal-notifier"))
    else:
        notifier = OsascriptNotifier(resolve_binary("osascript"))

    repos = _repo_cache(loaded, git_binary)
    github = GhCliGitHub(gh_binary)
    state = StateStore(loaded.var_dir / "state.db", clock=utc_now)
    program = launchd.venv_program(loaded)
    pipeline = Pipeline(
        config=settings,
        github=github,
        repos=repos,
        reviewer=reviewer,
        notifier=notifier,
        state=state,
        auth=AuthChecker(
            state=state,
            notifier=notifier,
            launcher=CommandLauncher(
                directory=loaded.var_dir / "auth",
                program=program,
                config_path=loaded.config_path,
            ),
        ),
        context=CliContextFetcher(settings.auth),
        approval_launcher=CommandLauncher(
            directory=loaded.var_dir / "approvals",
            program=program,
            config_path=loaded.config_path,
        ),
        clock=utc_now,
        dry_run=opts.dry_run,
    )
    feedback = FeedbackWatcher(
        config=settings, github=github, notifier=notifier, state=state, clock=utc_now
    )
    return Runtime(
        loaded=loaded,
        pipeline=pipeline,
        feedback=feedback,
        github=github,
        notifier=notifier,
        state=state,
        repos=repos,
    )


@dataclass(frozen=True)
class CycleResult:
    reviews: RunSummary
    feedback: FeedbackSummary


async def _run_cycle(opts: Options, loaded: LoadedConfig) -> CycleResult:
    runtime = await _build_runtime(opts, loaded)
    await runtime.repos.prune()
    await runtime.repos.prune_related(loaded.settings.all_related)
    # A run may outlast the interval; the run lock makes the next tick skip instead of overlap.
    deadline_s = max(
        loaded.settings.interval_minutes * 60 - 60, loaded.settings.reviewer.timeout_s + 300
    )
    try:
        async with asyncio.timeout(deadline_s):
            # Independent stages: a failure in one must not cancel the other.
            reviews, feedback = await asyncio.gather(
                runtime.pipeline.run(), runtime.feedback.run(), return_exceptions=True
            )
    except TimeoutError as exc:
        await _report_stage_failure(runtime, "run", exc)
        raise
    if isinstance(reviews, BaseException):
        await _report_stage_failure(runtime, "reviews", reviews)
    if isinstance(feedback, BaseException):
        await _report_stage_failure(runtime, "feedback", feedback)
    if isinstance(reviews, BaseException):
        raise reviews
    if isinstance(feedback, BaseException):
        raise feedback
    return CycleResult(reviews=reviews, feedback=feedback)


async def _report_stage_failure(runtime: Runtime, stage: str, exc: BaseException) -> None:
    logger.error("stage_failed stage=%s", stage, exc_info=exc)
    short = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
    if runtime.state.should_notify_error(stage, short):
        await notify_logging_failures(
            runtime.notifier,
            Notification(
                title=ERROR_TITLE,
                message=f"{stage}: {short}",
                group="review-watcher",
                open_url=None,
            ),
        )


@app.command()
def run(ctx: typer.Context) -> None:
    """Run one review cycle (what launchd invokes)."""
    opts = _options(ctx)
    loaded = _load(opts)
    try:
        with run_lock(loaded.var_dir / "run.lock"):
            result = asyncio.run(_run_cycle(opts, loaded))
    except RunLockHeld:
        logger.info("run_skipped reason=lock_held")
        return
    except Exception as exc:
        typer.echo(f"review-watcher run failed: {str(exc) or type(exc).__name__}", err=True)
        raise typer.Exit(code=1) from exc
    for outcome in result.reviews.outcomes:
        logger.info("pr_outcome pr=%s action=%s", outcome.slug, outcome.action.value)
    for feedback in result.feedback.outcomes:
        logger.info("feedback_outcome pr=%s new_items=%d", feedback.slug, feedback.new_items)


@app.command()
def review(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="GitHub PR URL.")],
    force: Annotated[bool, typer.Option("--force", help="Ignore recorded state.")] = False,
) -> None:
    """Review one PR now. Posts a pending review unless --dry-run is set."""
    opts = _options(ctx)
    loaded = _load(opts)
    target = _parse_pr_url(url, loaded)

    async def _go() -> tuple[PullRequestRef, RunOutcome]:
        runtime = await _build_runtime(opts, loaded)
        pr = await runtime.github.fetch_pull_request(target.owner, target.repo, target.number)
        outcome = await runtime.pipeline.review_one(pr, force=force)
        return pr, outcome

    _, outcome = asyncio.run(_go())
    if outcome.final is not None and opts.dry_run:
        typer.echo(outcome.final.model_dump_json(indent=2))
    typer.echo(f"{outcome.slug}: {outcome.action.value}", err=True)


@app.command()
def approve(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="GitHub PR URL.")],
) -> None:
    """Approve reviewing a PR's current head; the next cycle reviews it."""
    opts = _options(ctx)
    loaded = _load(opts)
    target = _parse_pr_url(url, loaded)

    async def _go() -> PullRequestRef:
        github = GhCliGitHub(resolve_binary("gh"))
        return await github.fetch_pull_request(target.owner, target.repo, target.number)

    pr = asyncio.run(_go())
    state = StateStore(loaded.var_dir / "state.db", clock=utc_now)
    state.approve(pr.full_name, pr.number, pr.head_sha)
    logger.info("pr_approved pr=%s head=%s", pr.slug, pr.head_sha)
    typer.echo(f"{pr.slug} by {pr.author}: {pr.title}")
    typer.echo(f"approved head {pr.head_sha[:12]}; it is reviewed on the next cycle")


@dataclass(frozen=True)
class _PrTarget:
    owner: str
    repo: str
    number: int


def _parse_pr_url(url: str, loaded: LoadedConfig) -> _PrTarget:
    match = PR_URL_RE.fullmatch(url.strip())
    if match is None:
        raise typer.BadParameter("expected https://github.com/<owner>/<repo>/pull/<number>")
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    if not loaded.settings.allows_repo(f"{owner}/{repo}"):
        raise typer.BadParameter(f"{owner}/{repo} is not in the configured repos allowlist")
    return _PrTarget(owner=owner, repo=repo, number=number)


@app.command()
def status(ctx: typer.Context) -> None:
    """List review-requested PRs and their recorded state."""
    opts = _options(ctx)
    loaded = _load(opts)
    settings = loaded.settings

    async def _go() -> None:
        runtime = await _build_runtime(opts, loaded)
        found = await runtime.github.search_review_requests(
            settings.github_login, settings.repo_names, settings.include_team_requests
        )
        if len(found) == 0:
            typer.echo("No open review requests in the allowlisted repos.")
        for pr in found:
            record = runtime.state.get(pr.full_name, pr.number, pr.head_sha)
            status_text = "not_seen" if record is None else record.status.value
            draft = " (draft)" if pr.is_draft else ""
            typer.echo(f"{pr.slug}{draft} head={pr.head_sha[:12]} state={status_text}")

    asyncio.run(_go())


@app.command("prune")
def prune_command(ctx: typer.Context) -> None:
    """Delete old state rows and orphaned worktrees."""
    opts = _options(ctx)
    loaded = _load(opts)
    state = StateStore(loaded.var_dir / "state.db", clock=utc_now)
    removed = state.prune(STATE_RETENTION)
    repos = _repo_cache(loaded, resolve_binary("git"))

    async def _prune() -> None:
        await repos.prune()
        await repos.prune_related(loaded.settings.all_related)

    asyncio.run(_prune())
    typer.echo(f"pruned state_rows={removed} worktrees=removed")


@app.command("auth-login")
def auth_login(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of an [[auth]] entry in config.")],
) -> None:
    """Run an auth entry's login command in this terminal, then re-run its check."""
    opts = _options(ctx)
    loaded = _load(opts)
    matches = [auth for auth in loaded.settings.auth if auth.name == name]
    if len(matches) == 0:
        raise typer.BadParameter(f"no [[auth]] entry named {name!r}")
    auth = matches[0]
    if auth.login is None:
        raise typer.BadParameter(f"[[auth]] {name!r} has no login command; set login in config")
    try:
        outcome = asyncio.run(sign_in(auth, trace=trace_to_stderr))
    except KeyboardInterrupt:
        typer.echo(f"{name}: sign-in cancelled", err=True)
        raise typer.Exit(code=130) from None
    if outcome.login_exit is None:
        typer.echo(f"{name}: already signed in")
        return
    if outcome.login_exit != 0:
        typer.echo(f"{name}: login exited {outcome.login_exit}", err=True)
    if not outcome.result.ok:
        typer.echo(f"{name}: check still failing ({outcome.result.detail})", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{name}: signed in; waiting reviews resume next cycle")


@app.command("install-agent")
def install_agent(ctx: typer.Context) -> None:
    """Write the LaunchAgent plist and (re)load it."""
    opts = _options(ctx)
    loaded = _load(opts)
    settings = loaded.settings
    program = launchd.venv_program(loaded)
    git_binary = resolve_binary("git")
    gh_binary = resolve_binary("gh")
    binaries = [gh_binary, git_binary]
    if settings.notifier.backend == "terminal_notifier":
        binaries.append(resolve_binary("terminal-notifier"))
    if not opts.no_llm:
        binaries.append(resolve_claude_binary())
    tools = {auth.check[0] for auth in settings.auth}
    tools.update(auth.login[0] for auth in settings.auth if auth.login is not None)
    tools.update(tool for source in settings.context.values() for tool in SOURCE_TOOLS[source.kind])
    binaries.extend(resolve_binary(tool) for tool in sorted(tools))

    first_repo = settings.repo_names[0]

    async def _ssh_check() -> None:
        await run_command(
            [str(git_binary), "ls-remote", f"git@github.com:{first_repo}.git", "HEAD"],
            env=base_env(),
            timeout_s=60,
        )

    asyncio.run(_ssh_check())

    plist_file = launchd.plist_path(Path.home())
    plist = launchd.build_plist(
        program=program,
        loaded=loaded,
        interval_minutes=settings.interval_minutes,
        path_env=launchd.build_path_env(binaries),
        home=Path.home(),
    )
    launchd.write_plist(plist_file, plist)

    async def _load_agent() -> None:
        await launchd.bootout(launchd.gui_domain(), plist_file)
        await launchd.bootstrap(launchd.gui_domain(), plist_file)

    asyncio.run(_load_agent())
    typer.echo(f"installed {plist_file}")
    typer.echo(f"kick it off with: launchctl kickstart -k {launchd.gui_domain()}/{launchd.LABEL}")
    typer.echo(f"logs: {loaded.var_dir / 'logs'}")


@app.command("uninstall-agent")
def uninstall_agent() -> None:
    """Unload and remove the LaunchAgent."""
    plist_file = launchd.plist_path(Path.home())

    async def _unload() -> None:
        await launchd.bootout(launchd.gui_domain(), plist_file)

    asyncio.run(_unload())
    if plist_file.exists():
        plist_file.unlink()
    typer.echo(f"removed {plist_file}")
