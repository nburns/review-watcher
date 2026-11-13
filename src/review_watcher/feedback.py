import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from review_watcher.config import Config
from review_watcher.github import GitHubClient
from review_watcher.models import FeedbackItem, FeedbackKind, PullRequestRef
from review_watcher.notifier import Notification, Notifier, notify_logging_failures
from review_watcher.state import StateStore

logger = logging.getLogger(__name__)

_REVIEW_STATE_RANK: dict[str, int] = {"CHANGES_REQUESTED": 0, "APPROVED": 1}
_KIND_RANK: dict[FeedbackKind, int] = {
    FeedbackKind.REVIEW: 0,
    FeedbackKind.INLINE_COMMENT: 1,
    FeedbackKind.COMMENT: 2,
}
_KIND_LABELS: dict[FeedbackKind, tuple[str, str]] = {
    FeedbackKind.REVIEW: ("review", "reviews"),
    FeedbackKind.INLINE_COMMENT: ("inline comment", "inline comments"),
    FeedbackKind.COMMENT: ("comment", "comments"),
}


@dataclass(frozen=True)
class FeedbackOutcome:
    slug: str
    new_items: int


@dataclass(frozen=True)
class FeedbackSummary:
    outcomes: tuple[FeedbackOutcome, ...]


class FeedbackWatcher:
    """Notifies about new reviews and comments from others on the user's own open PRs."""

    def __init__(
        self,
        *,
        config: Config,
        github: GitHubClient,
        notifier: Notifier,
        state: StateStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._github = github
        self._notifier = notifier
        self._state = state
        self._clock = clock

    async def run(self) -> FeedbackSummary:
        config = self._config
        if not config.feedback.enabled:
            return FeedbackSummary(outcomes=())
        since = self._state.feedback_since()
        prs = [
            pr
            for pr in await self._github.search_authored_prs(config.repo_names)
            if config.allows_repo(pr.full_name)
        ]
        logger.info("feedback_discovered count=%d", len(prs))
        ignored = {config.github_login.lower()} | {
            author.lower() for author in config.feedback.ignore_authors
        }
        outcomes: list[FeedbackOutcome] = []
        for pr in prs:
            items = [
                item
                for item in await self._github.list_feedback(pr)
                if item.author.lower() not in ignored and item.created_at >= since
            ]
            unseen = self._state.unseen_feedback_keys(item.key for item in items)
            fresh = [item for item in items if item.key in unseen]
            if len(fresh) > 0 and self._still_active(fresh):
                logger.info("feedback_waiting pr=%s items=%d", pr.slug, len(fresh))
                outcomes.append(FeedbackOutcome(slug=pr.slug, new_items=0))
                continue
            if len(fresh) > 0:
                await notify_logging_failures(self._notifier, _feedback_notification(pr, fresh))
                self._state.mark_feedback_seen(pr.full_name, pr.number, unseen)
                logger.info("feedback_new pr=%s items=%d", pr.slug, len(fresh))
            outcomes.append(FeedbackOutcome(slug=pr.slug, new_items=len(fresh)))
        return FeedbackSummary(outcomes=tuple(outcomes))

    def _still_active(self, fresh: Sequence[FeedbackItem]) -> bool:
        # Batch a reviewer's burst of comments: wait for a quiet period, but never past max_wait.
        settings = self._config.feedback
        now = self._clock()
        newest = max(item.created_at for item in fresh)
        oldest = min(item.created_at for item in fresh)
        quiet = now - newest >= timedelta(minutes=settings.quiet_minutes)
        overdue = now - oldest >= timedelta(minutes=settings.max_wait_minutes)
        return not (quiet or overdue)


def pick_target(items: Sequence[FeedbackItem]) -> FeedbackItem:
    """The item the notification should open: the most decisive review, else the oldest comment."""
    return min(
        items,
        key=lambda item: (
            _KIND_RANK[item.kind],
            _REVIEW_STATE_RANK.get(item.review_state or "", len(_REVIEW_STATE_RANK)),
            item.created_at,
        ),
    )


def _feedback_notification(pr: PullRequestRef, items: Sequence[FeedbackItem]) -> Notification:
    target = pick_target(items)
    if target.review_state == "CHANGES_REQUESTED":
        headline = "Changes requested"
    elif target.review_state == "APPROVED":
        headline = "Approved"
    else:
        headline = "New feedback"
    authors = sorted({item.author for item in items})
    return Notification(
        title=f"{headline}: {pr.slug}",
        subtitle=f"{_describe_counts(items)} from {', '.join(authors)}",
        message=pr.title,
        group=f"feedback:{pr.slug}",
        open_url=target.url,
    )


def _describe_counts(items: Sequence[FeedbackItem]) -> str:
    parts: list[str] = []
    for kind, (singular, plural) in _KIND_LABELS.items():
        count = sum(1 for item in items if item.kind == kind)
        if count > 0:
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)
