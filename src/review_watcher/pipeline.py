import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from review_watcher.config import Config
from review_watcher.diffmap import parse_diff
from review_watcher.github import GitHubClient
from review_watcher.guidance import collect_guidance
from review_watcher.guidance_changes import collect_guidance_changes
from review_watcher.models import (
    FinalReview,
    PullRequestRef,
    ReviewContext,
    Tier,
)
from review_watcher.notifier import Notification, Notifier, notify_logging_failures
from review_watcher.postprocess import MARKER_PREFIX, finalize, fold_inline_into_body
from review_watcher.proc import CommandFailed
from review_watcher.repo_cache import RepoCache
from review_watcher.reviewer import Reviewer
from review_watcher.state import ReviewRecord, ReviewStatus, StateStore

logger = logging.getLogger(__name__)
ERROR_TITLE = "review-watcher failed"
PENDING_STATE = "PENDING"
SKIP_STATUSES = (ReviewStatus.POSTED, ReviewStatus.SKIPPED_EXISTING_PENDING)


@dataclass(frozen=True)
class _Posted:
    review_id: int
    html_url: str


class Action(StrEnum):
    ALREADY_HANDLED = "already_handled"
    FAILURE_LIMIT_REACHED = "failure_limit_reached"
    REREVIEW_DISABLED = "rereview_disabled"
    DEBOUNCED = "debounced"
    POSTED = "posted"
    DRY_RUN = "dry_run"
    SKIPPED_EXISTING_PENDING = "skipped_existing_pending"
    FAILED = "failed"


@dataclass(frozen=True)
class RunOutcome:
    slug: str
    action: Action
    error: str | None = None
    final: FinalReview | None = None


@dataclass(frozen=True)
class RunSummary:
    outcomes: tuple[RunOutcome, ...]


class Pipeline:
    def __init__(
        self,
        *,
        config: Config,
        github: GitHubClient,
        repos: RepoCache,
        reviewer: Reviewer,
        notifier: Notifier,
        state: StateStore,
        clock: Callable[[], datetime],
        dry_run: bool,
    ) -> None:
        self._config = config
        self._github = github
        self._repos = repos
        self._reviewer = reviewer
        self._notifier = notifier
        self._state = state
        self._clock = clock
        self._dry_run = dry_run

    async def run(self) -> RunSummary:
        config = self._config
        found = await self._github.search_review_requests(
            config.github_login, config.repos, config.include_team_requests
        )
        candidates: list[PullRequestRef] = []
        for pr in found:
            if not config.allows_repo(pr.full_name):
                continue
            if config.skip_drafts and pr.is_draft:
                logger.info("pr_skipped reason=draft pr=%s", pr.slug)
                continue
            candidates.append(pr)
        logger.info("run_discovered count=%d", len(candidates))

        semaphore = asyncio.Semaphore(config.concurrency)
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._guarded(pr, semaphore)) for pr in candidates]
        return RunSummary(outcomes=tuple(task.result() for task in tasks))

    async def review_one(self, pr: PullRequestRef, *, force: bool) -> RunOutcome:
        return await self._process(pr, force=force, debounce=False)

    async def _guarded(self, pr: PullRequestRef, semaphore: asyncio.Semaphore) -> RunOutcome:
        async with semaphore:
            try:
                return await self._process(pr, force=False, debounce=True)
            except Exception as exc:
                logger.exception("pr_failed pr=%s", pr.slug)
                await self._record_failure(pr, str(exc))
                return RunOutcome(slug=pr.slug, action=Action.FAILED, error=str(exc))

    async def _process(self, pr: PullRequestRef, *, force: bool, debounce: bool) -> RunOutcome:
        config = self._config
        record = self._state.get(pr.full_name, pr.number, pr.head_sha)
        if not force:
            skip = self._skip_action(pr, record)
            if skip is not None:
                return RunOutcome(slug=pr.slug, action=skip)

        now = self._clock()
        if record is None:
            record = self._new_record(pr, ReviewStatus.PENDING_DEBOUNCE, now)
            self._state.save(record)
        if debounce and now - record.first_seen_at < timedelta(minutes=config.debounce_minutes):
            logger.info("pr_debounced pr=%s first_seen=%s", pr.slug, record.first_seen_at)
            return RunOutcome(slug=pr.slug, action=Action.DEBOUNCED)

        final = await self._build_review(pr)
        if self._dry_run:
            await self._notify(_review_notification(pr, final, dry_run=True))
            logger.info("pr_dry_run pr=%s risk=%s", pr.slug, final.risk.value)
            return RunOutcome(slug=pr.slug, action=Action.DRY_RUN, final=final)

        if await self._blocked_by_existing_pending(pr):
            self._state.save(
                self._record_for(pr, record, ReviewStatus.SKIPPED_EXISTING_PENDING, final)
            )
            await self._notify(_existing_pending_notification(pr))
            logger.info("pr_skipped reason=existing_pending pr=%s", pr.slug)
            return RunOutcome(slug=pr.slug, action=Action.SKIPPED_EXISTING_PENDING, final=final)

        posted = await self._post(pr, final)
        self._state.save(
            self._record_for(
                pr,
                record,
                ReviewStatus.POSTED,
                final,
                review_id=posted.review_id,
                review_url=posted.html_url,
            )
        )
        await self._notify(_review_notification(pr, final, dry_run=False))
        logger.info(
            "pr_posted pr=%s risk=%s review_id=%d", pr.slug, final.risk.value, posted.review_id
        )
        return RunOutcome(slug=pr.slug, action=Action.POSTED, final=final)

    def _skip_action(self, pr: PullRequestRef, record: ReviewRecord | None) -> Action | None:
        if record is not None:
            if record.status in SKIP_STATUSES:
                return Action.ALREADY_HANDLED
            if (
                record.status == ReviewStatus.FAILED
                and record.attempts >= self._config.max_attempts
            ):
                logger.warning("pr_skipped reason=failure_limit pr=%s", pr.slug)
                return Action.FAILURE_LIMIT_REACHED
            return None
        if not self._config.rereview_on_push:
            history = self._state.history(pr.full_name, pr.number)
            if any(item.status == ReviewStatus.POSTED for item in history):
                logger.info("pr_skipped reason=rereview_disabled pr=%s", pr.slug)
                return Action.REREVIEW_DISABLED
        return None

    async def _build_review(self, pr: PullRequestRef) -> FinalReview:
        config = self._config
        diff_text = await self._github.get_diff(pr)
        diff = parse_diff(diff_text)
        async with self._repos.worktree(pr) as worktree:
            guidance_changes = await collect_guidance_changes(
                self._repos, pr, diff, config.guidance_watch
            )
            guidance = await collect_guidance(self._repos, pr, diff, config.guidance)
            clipped, truncated = _clip_diff(diff_text, config.reviewer.max_diff_bytes)
            ctx = ReviewContext(
                pr=pr,
                diff_text=clipped,
                diff_truncated=truncated,
                changed_files=list(diff.changed_files),
                worktree_path=worktree,
                guidance=guidance,
                guidance_changes=guidance_changes,
                max_inline_comments=config.reviewer.max_inline_comments,
            )
            draft = await self._reviewer.review(ctx)
        return finalize(
            draft=draft,
            ctx=ctx,
            diff=diff,
            sensitive_paths=config.sensitive_paths,
        )

    async def _blocked_by_existing_pending(self, pr: PullRequestRef) -> bool:
        reviews = await self._github.list_reviews(pr)
        mine = [
            review
            for review in reviews
            if review.state == PENDING_STATE and review.author == self._config.github_login
        ]
        if not mine:
            return False
        if self._config.existing_pending_policy == "skip":
            return True
        for review in mine:
            if MARKER_PREFIX not in review.body:
                return True
        for review in mine:
            logger.info("pending_review_replaced pr=%s review_id=%d", pr.slug, review.review_id)
            await self._github.delete_review(pr, review.review_id)
        return False

    async def _post(self, pr: PullRequestRef, final: FinalReview) -> _Posted:
        try:
            posted = await self._github.create_pending_review(pr, final)
        except CommandFailed as exc:
            if "HTTP 422" not in exc.stderr or not final.inline_comments:
                raise
            logger.warning(
                "review_422_fallback pr=%s inline=%d", pr.slug, len(final.inline_comments)
            )
            posted = await self._github.create_pending_review(pr, fold_inline_into_body(final))
        return _Posted(review_id=posted.review_id, html_url=posted.html_url)

    def _new_record(self, pr: PullRequestRef, status: ReviewStatus, now: datetime) -> ReviewRecord:
        return ReviewRecord(
            repo=pr.full_name,
            pr_number=pr.number,
            head_sha=pr.head_sha,
            status=status,
            review_id=None,
            review_url=None,
            risk=None,
            guidance_changed=False,
            guidance_tier_max=None,
            error=None,
            attempts=0,
            first_seen_at=now,
            created_at=now,
            updated_at=now,
        )

    def _record_for(
        self,
        pr: PullRequestRef,
        previous: ReviewRecord,
        status: ReviewStatus,
        final: FinalReview,
        *,
        review_id: int | None = None,
        review_url: str | None = None,
    ) -> ReviewRecord:
        tier = final.guidance_tier_max
        return ReviewRecord(
            repo=pr.full_name,
            pr_number=pr.number,
            head_sha=pr.head_sha,
            status=status,
            review_id=review_id,
            review_url=review_url,
            risk=final.risk.value,
            guidance_changed=final.guidance_changed,
            guidance_tier_max=None if tier is None else tier.value,
            error=None,
            attempts=previous.attempts,
            first_seen_at=previous.first_seen_at,
            created_at=previous.created_at,
            updated_at=self._clock(),
        )

    async def _record_failure(self, pr: PullRequestRef, error: str) -> None:
        now = self._clock()
        previous = self._state.get(pr.full_name, pr.number, pr.head_sha)
        base = previous or self._new_record(pr, ReviewStatus.FAILED, now)
        self._state.save(
            ReviewRecord(
                repo=base.repo,
                pr_number=base.pr_number,
                head_sha=base.head_sha,
                status=ReviewStatus.FAILED,
                review_id=None,
                review_url=None,
                risk=None,
                guidance_changed=False,
                guidance_tier_max=None,
                error=error,
                attempts=base.attempts + 1,
                first_seen_at=base.first_seen_at,
                created_at=base.created_at,
                updated_at=now,
            )
        )
        short = error.splitlines()[0][:200] if error else "unknown error"
        if self._state.should_notify_error(pr.slug, short):
            await self._notify(
                Notification(
                    title=ERROR_TITLE,
                    message=f"{pr.slug}: {short}",
                    group=pr.slug,
                    open_url=pr.url,
                )
            )

    async def _notify(self, notification: Notification) -> None:
        await notify_logging_failures(self._notifier, notification)


def _clip_diff(diff_text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = diff_text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return diff_text, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    cut = clipped.rfind("\n")
    return (clipped[:cut] if cut > 0 else clipped), True


def _review_notification(pr: PullRequestRef, final: FinalReview, *, dry_run: bool) -> Notification:
    prefix = ""
    if final.guidance_changed:
        prefix += "CONFIG CHANGED · "
    if dry_run:
        prefix = "(dry run) " + prefix
    title = f"{prefix}{final.risk.value.upper()} risk: {pr.slug}"
    subtitle: str | None = None
    if final.guidance_changed:
        has_agent = final.guidance_tier_max == Tier.AGENT_EXECUTION_CONFIG
        subtitle = (
            f"Modifies {len(final.guidance_changes)} review/agent config files "
            f"(settings/hooks: {'yes' if has_agent else 'no'})"
        )
    return Notification(
        title=title,
        message=pr.title,
        group=pr.slug,
        open_url=f"{pr.url}/files",
        subtitle=subtitle,
    )


def _existing_pending_notification(pr: PullRequestRef) -> Notification:
    return Notification(
        title=f"Pending review exists: {pr.slug}",
        message="Skipped: you already have a pending review on this PR.",
        group=pr.slug,
        open_url=pr.url,
    )
