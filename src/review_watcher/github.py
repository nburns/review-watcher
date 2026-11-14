import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from review_watcher.models import (
    FeedbackItem,
    FeedbackKind,
    FinalReview,
    PostedReview,
    PullRequestRef,
    ReviewSummary,
)
from review_watcher.proc import CommandFailed, CommandTimeout, base_env, run_command

GH_TIMEOUT_S = 60.0
RETRY_DELAYS_S: tuple[float, ...] = (2.0, 10.0)
_TRANSIENT_GH_ERROR_RE = re.compile(
    r"TLS handshake timeout|i/o timeout|connection reset|connection refused|no such host"
    r"|unexpected EOF|Client\.Timeout|HTTP 50[234]",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)
SEARCH_PAGE_SIZE = 100

_SEARCH_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    issueCount
    pageInfo { hasNextPage }
    nodes {
      ... on PullRequest {
        number
        url
        title
        body
        isDraft
        headRefOid
        headRefName
        baseRefName
        baseRefOid
        author { login }
        repository { nameWithOwner owner { login } name defaultBranchRef { name } }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryInfo:
    full_name: str
    default_branch: str


@dataclass(frozen=True)
class PullRequestWithState:
    pr: PullRequestRef
    state: str


class GitHubClient(Protocol):
    async def search_review_requests(
        self, login: str, repos: Sequence[str], include_team_requests: bool
    ) -> list[PullRequestRef]: ...

    async def search_authored_prs(self, repos: Sequence[str]) -> list[PullRequestRef]: ...

    async def list_feedback(self, pr: PullRequestRef) -> list[FeedbackItem]: ...

    async def fetch_pull_request(self, owner: str, repo: str, number: int) -> PullRequestRef: ...

    async def fetch_pull_request_with_state(
        self, owner: str, repo: str, number: int
    ) -> PullRequestWithState: ...

    async def fetch_repository(self, full_name: str) -> RepositoryInfo: ...

    async def list_open_prs_with_head(
        self, owner: str, repo: str, branch: str
    ) -> list[PullRequestRef]: ...

    async def get_diff(self, pr: PullRequestRef) -> str: ...

    async def list_reviews(self, pr: PullRequestRef) -> list[ReviewSummary]: ...

    async def create_pending_review(
        self, pr: PullRequestRef, review: FinalReview
    ) -> PostedReview: ...

    async def delete_review(self, pr: PullRequestRef, review_id: int) -> None: ...


class GhCliGitHub:
    def __init__(
        self,
        gh_binary: Path,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gh = str(gh_binary)
        self._env = base_env()
        self._sleep = sleep

    async def _gh_run(
        self, args: Sequence[str], stdin: bytes | None = None, *, retry: bool = True
    ) -> bytes:
        # Writes are never retried: a timeout after the request was sent could post twice.
        delays = RETRY_DELAYS_S if retry else ()
        for attempt, delay in enumerate((*delays, None), start=1):
            try:
                result = await run_command(
                    [self._gh, *args],
                    env=self._env,
                    timeout_s=GH_TIMEOUT_S,
                    stdin=stdin,
                )
            except (CommandFailed, CommandTimeout) as exc:
                if delay is None or not is_transient(exc):
                    raise
                logger.warning(
                    "gh_retry attempt=%d delay_s=%.0f error=%s",
                    attempt,
                    delay,
                    str(exc).splitlines()[0],
                )
                await self._sleep(delay)
                continue
            return result.stdout
        raise AssertionError("unreachable: final attempt either returns or raises")

    async def _api_json(
        self, args: Sequence[str], stdin: bytes | None = None, *, retry: bool = True
    ) -> Any:
        raw = await self._gh_run(["api", *args], stdin=stdin, retry=retry)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh api returned non-JSON output for {args[0]!r}") from exc

    async def search_review_requests(
        self, login: str, repos: Sequence[str], include_team_requests: bool
    ) -> list[PullRequestRef]:
        requested = "review-requested:@me" if include_team_requests else "user-review-requested:@me"
        return await self._search_prs(requested, repos)

    async def search_authored_prs(self, repos: Sequence[str]) -> list[PullRequestRef]:
        return await self._search_prs("author:@me", repos)

    async def _search_prs(self, qualifier: str, repos: Sequence[str]) -> list[PullRequestRef]:
        found: dict[str, PullRequestRef] = {}
        for repo in repos:
            query = f"is:pr is:open archived:false {qualifier} repo:{repo}"
            payload = json.dumps({"query": _SEARCH_QUERY, "variables": {"q": query}}).encode()
            data = await self._api_json(["graphql", "--input", "-"], stdin=payload)
            _raise_on_graphql_errors(data, repo)
            search = data["data"]["search"]
            if search["pageInfo"]["hasNextPage"]:
                raise GitHubError(
                    f"PR search {qualifier!r} for {repo} exceeded {SEARCH_PAGE_SIZE} results"
                )
            for node in search["nodes"]:
                pr = _pr_from_search_node(node)
                found[pr.slug] = pr
        return list(found.values())

    async def list_feedback(self, pr: PullRequestRef) -> list[FeedbackItem]:
        base = f"repos/{pr.owner}/{pr.repo}"
        reviews = await self._api_pages([f"{base}/pulls/{pr.number}/reviews"])
        inline = await self._api_pages([f"{base}/pulls/{pr.number}/comments"])
        conversation = await self._api_pages([f"{base}/issues/{pr.number}/comments"])
        items: list[FeedbackItem] = []
        for item in reviews:
            state = item["state"]
            if state == "PENDING":
                continue
            # Inline-only COMMENTED reviews are represented by their inline comments instead.
            if state == "COMMENTED" and (item["body"] or "") == "":
                continue
            items.append(
                FeedbackItem.model_validate(
                    {
                        "kind": FeedbackKind.REVIEW,
                        "item_id": item["id"],
                        "author": _login(item["user"]),
                        "review_state": state,
                        "url": item["html_url"],
                        "created_at": item["submitted_at"],
                    }
                )
            )
        for kind, comments in (
            (FeedbackKind.INLINE_COMMENT, inline),
            (FeedbackKind.COMMENT, conversation),
        ):
            items.extend(
                FeedbackItem.model_validate(
                    {
                        "kind": kind,
                        "item_id": item["id"],
                        "author": _login(item["user"]),
                        "review_state": None,
                        "url": item["html_url"],
                        "created_at": item["created_at"],
                    }
                )
                for item in comments
            )
        return items

    async def _api_pages(self, args: Sequence[str]) -> list[dict[str, Any]]:
        pages = await self._api_json([*args, "--paginate", "--slurp"])
        return [item for page in pages for item in page]

    async def fetch_pull_request(self, owner: str, repo: str, number: int) -> PullRequestRef:
        data = await self._api_json([f"repos/{owner}/{repo}/pulls/{number}"])
        return _pr_from_rest(data, owner, repo)

    async def fetch_pull_request_with_state(
        self, owner: str, repo: str, number: int
    ) -> PullRequestWithState:
        data = await self._api_json([f"repos/{owner}/{repo}/pulls/{number}"])
        state = "merged" if data["merged_at"] is not None else data["state"]
        return PullRequestWithState(pr=_pr_from_rest(data, owner, repo), state=state)

    async def fetch_repository(self, full_name: str) -> RepositoryInfo:
        data = await self._api_json([f"repos/{full_name}"])
        return RepositoryInfo(full_name=data["full_name"], default_branch=data["default_branch"])

    async def list_open_prs_with_head(
        self, owner: str, repo: str, branch: str
    ) -> list[PullRequestRef]:
        items = await self._api_pages(
            [
                f"repos/{owner}/{repo}/pulls",
                "--method",
                "GET",
                "-f",
                "state=open",
                "-f",
                f"head={owner}:{branch}",
            ]
        )
        return [_pr_from_rest(item, owner, repo) for item in items]

    async def get_diff(self, pr: PullRequestRef) -> str:
        raw = await self._gh_run(
            ["pr", "diff", str(pr.number), "--repo", pr.full_name, "--color", "never"]
        )
        return raw.decode("utf-8", errors="replace")

    async def list_reviews(self, pr: PullRequestRef) -> list[ReviewSummary]:
        items = await self._api_pages([f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"])
        return [
            ReviewSummary(
                review_id=item["id"],
                state=item["state"],
                author=_login(item["user"]),
                body=item["body"] or "",
            )
            for item in items
        ]

    async def create_pending_review(self, pr: PullRequestRef, review: FinalReview) -> PostedReview:
        comments: list[dict[str, Any]] = []
        for comment in review.inline_comments:
            entry: dict[str, Any] = {
                "path": comment.path,
                "line": comment.line,
                "side": comment.side.value,
                "body": comment.body,
            }
            if comment.start_line is not None:
                entry["start_line"] = comment.start_line
                entry["start_side"] = comment.side.value
            comments.append(entry)
        payload = {"commit_id": pr.head_sha, "body": review.body, "comments": comments}
        data = await self._api_json(
            [
                f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
                "--method",
                "POST",
                "--input",
                "-",
            ],
            stdin=json.dumps(payload).encode(),
            retry=False,
        )
        return PostedReview(review_id=data["id"], html_url=data["html_url"])

    async def delete_review(self, pr: PullRequestRef, review_id: int) -> None:
        await self._gh_run(
            [
                "api",
                f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews/{review_id}",
                "--method",
                "DELETE",
            ],
            retry=False,
        )


def is_transient(exc: CommandFailed | CommandTimeout) -> bool:
    if isinstance(exc, CommandTimeout):
        return True
    return _TRANSIENT_GH_ERROR_RE.search(exc.stderr) is not None


def _raise_on_graphql_errors(data: Any, repo: str) -> None:
    if not isinstance(data, dict):
        raise GitHubError(f"unexpected GraphQL response for {repo}")
    payload = cast("dict[str, object]", data)
    errors = payload.get("errors")
    if errors is not None:
        raise GitHubError(f"GraphQL errors for {repo}: {errors}")
    if "data" not in payload:
        raise GitHubError(f"GraphQL response for {repo} has no data key")


def _login(user: dict[str, Any] | None) -> str:
    # GitHub returns a null user for deleted accounts.
    return "ghost" if user is None else user["login"]


def _pr_from_rest(data: dict[str, Any], owner: str, repo: str) -> PullRequestRef:
    return PullRequestRef(
        owner=owner,
        repo=repo,
        number=data["number"],
        head_sha=data["head"]["sha"],
        head_ref=data["head"]["ref"],
        base_ref=data["base"]["ref"],
        base_sha=data["base"]["sha"],
        default_branch=data["base"]["repo"]["default_branch"],
        url=data["html_url"],
        title=data["title"],
        author=_login(data["user"]),
        is_draft=data["draft"],
        body=data["body"],
    )


def _pr_from_search_node(node: dict[str, Any]) -> PullRequestRef:
    repository = node["repository"]
    author = _login(node["author"])
    return PullRequestRef(
        owner=repository["owner"]["login"],
        repo=repository["name"],
        number=node["number"],
        head_sha=node["headRefOid"],
        head_ref=node["headRefName"],
        base_ref=node["baseRefName"],
        base_sha=node["baseRefOid"],
        default_branch=repository["defaultBranchRef"]["name"],
        url=node["url"],
        title=node["title"],
        author=author,
        is_draft=node["isDraft"],
        body=node["body"],
    )
