from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from review_watcher.config import Config
from review_watcher.models import ReviewSummary
from review_watcher.pipeline import ERROR_TITLE, Action, Pipeline
from review_watcher.state import ReviewStatus, StateStore
from tests.fakes import (
    GIT,
    FakeGitHub,
    FakeNotifier,
    FakeRepoCache,
    FakeReviewer,
    LocalRepo,
    commit_files,
    diff_between,
    empty_draft,
    git,
    make_config,
    make_local_repo,
    make_pr,
    publish,
)

BASE_FILES = {"README.md": "readme\n", "src/app.py": "a = 1\n"}


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    repo: LocalRepo
    base: str
    head: str
    clock: Clock
    state: StateStore
    notifier: FakeNotifier
    reviewer: FakeReviewer
    github: FakeGitHub

    def pipeline(self, config: Config, *, dry_run: bool = False) -> Pipeline:
        repos = FakeRepoCache(
            git_binary=Path(GIT),
            bare_root=self.repo.bare_root,
            worktree_root=self.repo.bare_root.parent / "wt",
        )
        return Pipeline(
            config=config,
            github=self.github,
            repos=repos,
            reviewer=self.reviewer,
            notifier=self.notifier,
            state=self.state,
            clock=self.clock,
            dry_run=dry_run,
        )


def _world(
    tmp_path: Path,
    *,
    head_files: dict[str, str] | None = None,
    prs: list[int] | None = None,
    fail_for: set[int] | None = None,
) -> World:
    repo = make_local_repo(tmp_path)
    (tmp_path / "wt").mkdir()
    base = commit_files(repo, BASE_FILES)
    head = commit_files(repo, head_files or BASE_FILES)
    publish(repo)
    pull_requests = [make_pr(number, head, base) for number in prs or [1]]
    clock = Clock(now=datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    return World(
        repo=repo,
        base=base,
        head=head,
        clock=clock,
        state=StateStore(tmp_path / "state.db", clock=clock),
        notifier=FakeNotifier(),
        reviewer=FakeReviewer(draft=empty_draft(), fail_for=fail_for or set()),
        github=FakeGitHub(pull_requests, diff_between(repo, base, head)),
    )


async def test_same_sha_is_reviewed_once(tmp_path: Path) -> None:
    world = _world(tmp_path)
    config = make_config()

    await world.pipeline(config).run()
    second = await world.pipeline(config).run()

    assert len(world.reviewer.contexts) == 1
    assert len(world.github.created) == 1
    assert second.outcomes[0].action == Action.ALREADY_HANDLED


async def test_new_push_is_reviewed_again(tmp_path: Path) -> None:
    world = _world(tmp_path)
    config = make_config()
    await world.pipeline(config).run()

    new_head = commit_files(world.repo, {**BASE_FILES, "src/app.py": "a = 2\n"})
    publish(world.repo)
    world.github.prs = [make_pr(1, new_head, world.base)]
    world.github.diff_text = diff_between(world.repo, world.base, new_head)
    await world.pipeline(config).run()

    assert len(world.reviewer.contexts) == 2
    assert len(world.github.created) == 2


async def test_debounce_waits_for_quiet_period(tmp_path: Path) -> None:
    world = _world(tmp_path)
    config = make_config(debounce_minutes=5)

    await world.pipeline(config).run()
    assert world.reviewer.contexts == []
    record = world.state.get("acme/app", 1, world.head)
    assert record is not None
    assert record.status == ReviewStatus.PENDING_DEBOUNCE

    world.clock.now += timedelta(minutes=4)
    await world.pipeline(config).run()
    assert world.reviewer.contexts == []

    world.clock.now += timedelta(minutes=1)
    await world.pipeline(config).run()
    assert len(world.reviewer.contexts) == 1
    assert len(world.github.created) == 1


async def test_dry_run_posts_nothing(tmp_path: Path) -> None:
    world = _world(tmp_path)

    summary = await world.pipeline(make_config(), dry_run=True).run()

    assert summary.outcomes[0].action == Action.DRY_RUN
    assert len(world.reviewer.contexts) == 1
    assert world.github.created == []
    assert world.notifier.sent[0].title.startswith("(dry run)")

    summary = await world.pipeline(make_config()).run()

    assert summary.outcomes[0].action == Action.POSTED
    assert len(world.github.created) == 1


async def test_reviewer_failure_fails_only_that_pr(tmp_path: Path) -> None:
    world = _world(tmp_path, prs=[1, 2], fail_for={1})

    summary = await world.pipeline(make_config()).run()

    actions = {outcome.slug: outcome.action for outcome in summary.outcomes}
    assert actions == {"acme/app#1": Action.FAILED, "acme/app#2": Action.POSTED}
    failed = world.state.get("acme/app", 1, world.head)
    assert failed is not None
    assert failed.status == ReviewStatus.FAILED
    assert failed.attempts == 1
    assert any(item.title == ERROR_TITLE for item in world.notifier.sent)


async def test_existing_pending_review_is_skipped_by_default(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.github.reviews[1] = [
        ReviewSummary(review_id=9, state="PENDING", author="octocat", body="old draft")
    ]

    summary = await world.pipeline(make_config()).run()

    assert summary.outcomes[0].action == Action.SKIPPED_EXISTING_PENDING
    assert world.github.created == []


async def test_guidance_change_adds_banner_floor_and_title(tmp_path: Path) -> None:
    head_files = {**BASE_FILES, ".claude/settings.json": '{"hooks": {}}\n'}
    world = _world(tmp_path, head_files=head_files)
    world.reviewer.draft = empty_draft(
        guidance_change_assessment="Adds a settings file that may configure hooks.",
    )

    await world.pipeline(make_config()).run()

    posted = world.github.created[0]
    assert posted.risk.value == "high"
    assert "Warning: this PR modifies review or agent configuration" in posted.body
    notification = world.notifier.sent[0]
    assert notification.title.startswith("CONFIG CHANGED")
    record = world.state.get("acme/app", 1, world.head)
    assert record is not None
    assert record.guidance_changed


async def test_timeouts_back_off_exponentially_and_never_exhaust_retries(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    world.reviewer.timeout_for = {1}
    config = make_config(max_attempts=1, interval_minutes=10)
    actions: list[Action] = []

    for _ in range(5):
        summary = await world.pipeline(config).run()
        actions.append(summary.outcomes[0].action)
        world.clock.now += timedelta(minutes=10)

    assert actions == [
        Action.FAILED,
        Action.FAILED,
        Action.TIMEOUT_BACKOFF,
        Action.FAILED,
        Action.TIMEOUT_BACKOFF,
    ]
    record = world.state.get("acme/app", 1, world.head)
    assert record is not None
    assert record.attempts == 0
    assert record.timeouts == 3


async def test_stacked_pr_gets_full_default_branch_diff_as_context(tmp_path: Path) -> None:
    repo = make_local_repo(tmp_path)
    (tmp_path / "wt").mkdir()
    base = commit_files(repo, BASE_FILES)
    publish(repo)
    parent_head = commit_files(repo, {**BASE_FILES, "src/lower.py": "lower = 1\n"})
    head = commit_files(
        repo, {**BASE_FILES, "src/lower.py": "lower = 1\n", "src/upper.py": "upper = 2\n"}
    )
    git(repo.work, "push", "-q", str(repo.bare), "HEAD:refs/heads/feature-2")
    git(repo.bare, "update-ref", "refs/rw/base/main", base)
    parent = make_pr(1, parent_head, base, head_ref="feature-1")
    pr = make_pr(2, head, parent_head, head_ref="feature-2", base_ref="feature-1")
    clock = Clock(now=datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    github = FakeGitHub([pr], diff_between(repo, parent_head, head))
    github.open_by_head["feature-1"] = [parent]
    world = World(
        repo=repo,
        base=base,
        head=head,
        clock=clock,
        state=StateStore(tmp_path / "state.db", clock=clock),
        notifier=FakeNotifier(),
        reviewer=FakeReviewer(draft=empty_draft()),
        github=github,
    )

    await world.pipeline(make_config()).run()

    ctx = world.reviewer.contexts[0]
    assert "src/upper.py" in ctx.diff_text
    assert "src/lower.py" not in ctx.diff_text
    assert ctx.stack is not None
    assert [ancestor.number for ancestor in ctx.stack.ancestors] == [1]
    assert "src/lower.py" in ctx.stack.diff_text
    assert "src/upper.py" in ctx.stack.diff_text
    assert "Stacked PR: merges into `feature-1` on top of #1" in github.created[0].body
