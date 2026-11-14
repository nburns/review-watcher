import logging
from dataclasses import dataclass

from review_watcher.models import Confidence, Finding, Quote, ReviewContext, ReviewDraft
from review_watcher.proc import CommandFailed
from review_watcher.repo_cache import RepoCache

MIN_QUOTE_CHARS = 10

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Source:
    full_name: str
    shas: tuple[str, ...]
    path: str


async def ground_findings(draft: ReviewDraft, ctx: ReviewContext, repos: RepoCache) -> ReviewDraft:
    kept: list[Finding] = []
    demoted: list[str] = []
    for finding in draft.findings:
        problem = await _quote_problem(finding, ctx, repos)
        if problem is None:
            kept.append(finding)
            continue
        logger.info("finding_demoted pr=%s path=%s reason=%s", ctx.pr.slug, finding.path, problem)
        demoted.append(f"{finding.title}: {finding.body} (Not posted as a finding: {problem}.)")
    if not demoted:
        return draft
    return draft.model_copy(
        update={"findings": kept, "questions_for_author": [*draft.questions_for_author, *demoted]}
    )


async def _quote_problem(finding: Finding, ctx: ReviewContext, repos: RepoCache) -> str | None:
    quote = finding.quote
    if quote is None:
        return None if finding.confidence == Confidence.LOW else "no quoted evidence"
    text = quote.text.strip()
    shape = _shape_problem(text)
    if shape is not None:
        return shape
    source = _resolve(quote, ctx)
    if source is None:
        return f"quoted evidence cites an unknown repository ({quote.path})"
    for sha in source.shas:
        try:
            content = await repos.read_path_at(source.full_name, sha, source.path)
        except CommandFailed:
            continue
        if text in content.decode("utf-8", errors="replace"):
            return None
    return f"quoted evidence was not found in {quote.path}"


def _shape_problem(text: str) -> str | None:
    if "\n" in text:
        return "quoted evidence spans lines"
    if len(text) < MIN_QUOTE_CHARS:
        return "quoted evidence is too short to verify"
    return None


def _resolve(quote: Quote, ctx: ReviewContext) -> _Source | None:
    repo_part, sep, rest = quote.path.partition(":")
    if sep and "/" in repo_part:
        if repo_part.lower() == ctx.pr.full_name.lower():
            return _Source(ctx.pr.full_name, (ctx.pr.head_sha, ctx.pr.base_sha), rest.lstrip("/"))
        if ctx.related is None:
            return None
        for repo in ctx.related.repos:
            if repo.full_name == repo_part.lower() and repo.checkout is not None:
                return _Source(repo.full_name, (repo.checkout.commit_sha,), rest.lstrip("/"))
        return None
    return _Source(ctx.pr.full_name, (ctx.pr.head_sha, ctx.pr.base_sha), quote.path.lstrip("/"))
