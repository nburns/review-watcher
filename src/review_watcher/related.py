import asyncio
import json
import logging
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from review_watcher.config import Config
from review_watcher.context import clip_text
from review_watcher.github import GitHubClient
from review_watcher.models import (
    LinkedPullRequest,
    LinkFailure,
    PullRequestRef,
    RelatedCheckout,
    RelatedContext,
    RelatedGuide,
    RelatedRepo,
)
from review_watcher.repo_cache import CheckoutState, GitError, RepoCache

MAX_LINKED_PRS = 5
GUIDE_NAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "README.md")
_SHORT_REF_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)\b")
_URL_REF_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullReference:
    full_name: str
    number: int

    @property
    def label(self) -> str:
        return f"{self.full_name}#{self.number}"


@dataclass(frozen=True)
class ReferenceScan:
    allowed: list[PullReference]
    ignored: list[PullReference]


def find_references(text: str, allowed_repos: Collection[str]) -> ReferenceScan:
    found: dict[PullReference, None] = {}
    matches = [*_SHORT_REF_RE.finditer(text), *_URL_REF_RE.finditer(text)]
    for match in sorted(matches, key=lambda item: item.start()):
        owner, repo, number = match.group(1), match.group(2), int(match.group(3))
        found.setdefault(PullReference(full_name=f"{owner}/{repo}".lower(), number=number))
    allowed = [ref for ref in found if ref.full_name in allowed_repos]
    ignored = [ref for ref in found if ref.full_name not in allowed_repos]
    return ReferenceScan(allowed=allowed, ignored=ignored)


class RelatedCollector:
    def __init__(
        self,
        *,
        config: Config,
        github: GitHubClient,
        repos: RepoCache,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._github = github
        self._repos = repos
        self._clock = clock
        # One refresh per repo per collector, so a checkout never moves under a running review.
        self._locks: dict[str, asyncio.Lock] = {}
        self._results: dict[str, RelatedRepo] = {}

    async def collect(self, pr: PullRequestRef) -> RelatedContext | None:
        names = self._config.related_for(pr.full_name)
        if not names:
            return None
        repos = await asyncio.gather(*(self._repo(name) for name in names))
        scan = find_references(pr.body, names) if pr.body is not None else None
        linked: list[LinkedPullRequest] = []
        failures: list[LinkFailure] = []
        if scan is not None:
            for ref in scan.ignored:
                logger.info("related_ref_ignored pr=%s ref=%s", pr.slug, ref.label)
            for ref in scan.allowed[MAX_LINKED_PRS:]:
                failures.append(
                    LinkFailure(reference=ref.label, error=f"over the {MAX_LINKED_PRS} PR limit")
                )
            for ref in scan.allowed[:MAX_LINKED_PRS]:
                try:
                    linked.append(await self._linked(ref))
                except Exception as exc:
                    # A missing linked PR must not block the review; the body names it.
                    logger.exception("related_link_failed pr=%s ref=%s", pr.slug, ref.label)
                    failures.append(LinkFailure(reference=ref.label, error=_first_line(exc)))
        return RelatedContext(repos=list(repos), linked_prs=linked, link_failures=failures)

    async def _repo(self, name: str) -> RelatedRepo:
        async with self._locks.setdefault(name, asyncio.Lock()):
            if name in self._results:
                return self._results[name]
            description = self._config.describe_repo(name)
            try:
                checkout = await self._checkout(name)
            except Exception as exc:
                # An unavailable related repo must not block the review; the body names it.
                logger.exception("related_checkout_failed repo=%s", name)
                result = RelatedRepo(
                    full_name=name, description=description, checkout=None, error=_first_line(exc)
                )
            else:
                result = RelatedRepo(
                    full_name=name, description=description, checkout=checkout, error=None
                )
            self._results[name] = result
            return result

    async def _checkout(self, name: str) -> RelatedCheckout:
        located = await self._current_or_refresh(name)
        state = located.state
        return RelatedCheckout(
            path=state.path,
            branch=located.branch,
            commit_sha=state.commit_sha,
            committed_at=state.committed_at,
            guide=await self._guide(name, state.commit_sha),
        )

    async def _current_or_refresh(self, name: str) -> "_Located":
        stamp = self._read_stamp(name)
        max_age = timedelta(hours=self._config.related_max_age_hours)
        if stamp is not None and self._clock() - stamp.refreshed_at < max_age:
            try:
                state = await self._repos.current_related(name)
            except GitError:
                logger.warning("related_checkout_missing repo=%s", name, exc_info=True)
            else:
                logger.info("related_reused repo=%s sha=%s", name, state.commit_sha[:12])
                return _Located(state=state, branch=stamp.branch)
        info = await self._github.fetch_repository(name)
        state = await self._repos.refresh_related(name, info.default_branch)
        self._write_stamp(name, _Stamp(refreshed_at=self._clock(), branch=info.default_branch))
        logger.info("related_refreshed repo=%s sha=%s", name, state.commit_sha[:12])
        return _Located(state=state, branch=info.default_branch)

    async def _guide(self, name: str, sha: str) -> RelatedGuide | None:
        entries = {entry.path: entry for entry in await self._repos.list_related_root(name, sha)}
        for guide_name in GUIDE_NAMES:
            entry = entries.get(guide_name)
            if entry is None or not entry.is_blob:
                continue
            raw = await self._repos.read_related_blob(name, sha, entry)
            budget = self._config.guidance.max_file_bytes
            return RelatedGuide(
                path=guide_name,
                text=raw[:budget].decode("utf-8", errors="replace"),
                truncated=len(raw) > budget,
            )
        return None

    async def _linked(self, ref: PullReference) -> LinkedPullRequest:
        owner, repo = ref.full_name.split("/")
        found = await self._github.fetch_pull_request_with_state(owner, repo, ref.number)
        diff_text = await self._github.get_diff(found.pr)
        clipped, truncated = clip_text(diff_text, self._config.reviewer.max_diff_bytes)
        return LinkedPullRequest(
            full_name=found.pr.full_name,
            number=found.pr.number,
            title=found.pr.title,
            state=found.state,
            url=found.pr.url,
            diff_text=clipped,
            diff_truncated=truncated,
        )

    def _read_stamp(self, name: str) -> "_Stamp | None":
        path = self._repos.related_stamp_path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _Stamp(
                refreshed_at=datetime.fromisoformat(data["refreshed_at"]), branch=data["branch"]
            )
        except (ValueError, KeyError, TypeError):
            logger.warning("related_stamp_unreadable repo=%s path=%s", name, path)
            return None

    def _write_stamp(self, name: str, stamp: "_Stamp") -> None:
        path = self._repos.related_stamp_path(name)
        payload = {"refreshed_at": stamp.refreshed_at.isoformat(), "branch": stamp.branch}
        path.write_text(json.dumps(payload), encoding="utf-8")


@dataclass(frozen=True)
class _Located:
    state: CheckoutState
    branch: str


@dataclass(frozen=True)
class _Stamp:
    refreshed_at: datetime
    branch: str


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:200] if text else type(exc).__name__
