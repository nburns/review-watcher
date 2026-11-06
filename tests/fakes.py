import asyncio
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from review_watcher.config import Config
from review_watcher.models import (
    FeedbackItem,
    FinalReview,
    PostedReview,
    PullRequestRef,
    ReviewContext,
    ReviewDraft,
    ReviewSummary,
)
from review_watcher.notifier import Notification
from review_watcher.proc import CommandTimeout
from review_watcher.repo_cache import RepoCache

GIT = shutil.which("git") or "git"


class FakeGitHub:
    def __init__(self, prs: Sequence[PullRequestRef], diff_text: str) -> None:
        self.prs = list(prs)
        self.diff_text = diff_text
        self.created: list[FinalReview] = []
        self.deleted: list[int] = []
        self.reviews: dict[int, list[ReviewSummary]] = {}
        self.authored: list[PullRequestRef] = []
        self.feedback: dict[int, list[FeedbackItem]] = {}
        self.open_by_head: dict[str, list[PullRequestRef]] = {}

    async def search_review_requests(
        self, login: str, repos: Sequence[str], include_team_requests: bool
    ) -> list[PullRequestRef]:
        return list(self.prs)

    async def search_authored_prs(self, repos: Sequence[str]) -> list[PullRequestRef]:
        return list(self.authored)

    async def list_feedback(self, pr: PullRequestRef) -> list[FeedbackItem]:
        return list(self.feedback.get(pr.number, []))

    async def fetch_pull_request(self, owner: str, repo: str, number: int) -> PullRequestRef:
        raise NotImplementedError

    async def list_open_prs_with_head(
        self, owner: str, repo: str, branch: str
    ) -> list[PullRequestRef]:
        return list(self.open_by_head.get(branch, []))

    async def get_diff(self, pr: PullRequestRef) -> str:
        return self.diff_text

    async def list_reviews(self, pr: PullRequestRef) -> list[ReviewSummary]:
        return list(self.reviews.get(pr.number, []))

    async def create_pending_review(self, pr: PullRequestRef, review: FinalReview) -> PostedReview:
        self.created.append(review)
        return PostedReview(review_id=500 + pr.number, html_url=f"{pr.url}#review")

    async def delete_review(self, pr: PullRequestRef, review_id: int) -> None:
        self.deleted.append(review_id)


@dataclass
class FakeReviewer:
    draft: ReviewDraft
    fail_for: set[int] = field(default_factory=set)
    timeout_for: set[int] = field(default_factory=set)
    hang_for: set[int] = field(default_factory=set)
    contexts: list[ReviewContext] = field(default_factory=list)

    async def review(self, ctx: ReviewContext) -> ReviewDraft:
        self.contexts.append(ctx)
        if ctx.pr.number in self.fail_for:
            raise RuntimeError("model backend exploded")
        if ctx.pr.number in self.timeout_for:
            raise CommandTimeout(["slow-tool"], 1.0)
        if ctx.pr.number in self.hang_for:
            await asyncio.Event().wait()
        return self.draft


@dataclass
class FakeNotifier:
    sent: list[Notification] = field(default_factory=list)

    async def notify(self, notification: Notification) -> None:
        self.sent.append(notification)


class FakeRepoCache(RepoCache):
    """Real git over a local bare repo; only the network fetch is skipped."""

    async def ensure_fetched(self, pr: PullRequestRef) -> None:
        return None


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT, "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class LocalRepo:
    work: Path
    bare: Path
    bare_root: Path


def make_local_repo(root: Path, owner: str = "acme", name: str = "app") -> LocalRepo:
    work = root / "work"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    bare_root = root / "repos"
    bare = bare_root / owner / f"{name}.git"
    bare.parent.mkdir(parents=True)
    return LocalRepo(work=work, bare=bare, bare_root=bare_root)


def commit_files(repo: LocalRepo, files: dict[str, str]) -> str:
    for child in repo.work.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for relative, content in files.items():
        target = repo.work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo.work, "add", "-A")
    git(repo.work, "commit", "-q", "--allow-empty", "-m", "snapshot")
    return git(repo.work, "rev-parse", "HEAD")


def publish(repo: LocalRepo) -> None:
    if not repo.bare.exists():
        git(repo.work, "clone", "-q", "--bare", str(repo.work), str(repo.bare))
    else:
        git(repo.work, "push", "-q", "--force", str(repo.bare), "HEAD:refs/heads/main")


def diff_between(repo: LocalRepo, base: str, head: str) -> str:
    return git(repo.bare, "diff", "--no-color", f"{base}...{head}") + "\n"


def make_pr(
    number: int,
    head_sha: str,
    base_sha: str,
    *,
    is_draft: bool = False,
    head_ref: str | None = None,
    base_ref: str = "main",
) -> PullRequestRef:
    return PullRequestRef(
        owner="acme",
        repo="app",
        number=number,
        head_sha=head_sha,
        head_ref=head_ref if head_ref is not None else f"feature-{number}",
        base_ref=base_ref,
        base_sha=base_sha,
        default_branch="main",
        url=f"https://github.com/acme/app/pull/{number}",
        title=f"PR {number}",
        author="octocat",
        is_draft=is_draft,
    )


def make_config(**overrides: object) -> Config:
    data: dict[str, object] = {
        "github_login": "octocat",
        "repos": ["acme/app"],
        "debounce_minutes": 0,
        "concurrency": 2,
    }
    data.update(overrides)
    return Config.model_validate(data)


def empty_draft(**overrides: object) -> ReviewDraft:
    data: dict[str, object] = {
        "summary": "Looks fine.",
        "proposed_risk": "low",
        "risk_rationale": "Small change.",
        "findings": [],
        "guidance_sources_used": [],
        "guidance_change_assessment": None,
        "questions_for_author": [],
    }
    data.update(overrides)
    return ReviewDraft.model_validate(data)
