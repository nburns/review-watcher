from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from review_watcher.models import (
    CONFIDENCE_RANK,
    SEVERITY_RANK,
    Finding,
    ReviewDraft,
    max_risk,
    strictest_suggestion,
)

DUPLICATE_LINE_WINDOW = 8


@dataclass(frozen=True)
class LensDraft:
    name: str
    draft: ReviewDraft


def merge_drafts(main: ReviewDraft, lenses: Sequence[LensDraft]) -> ReviewDraft:
    findings = list(main.findings)
    for lens in lenses:
        absorbed: set[int] = set()
        for finding in lens.draft.findings:
            _add_finding(findings, finding, main=main.findings, absorbed=absorbed)
    risk = main.proposed_risk
    suggestion = main.suggestion
    for lens in lenses:
        risk = max_risk(risk, lens.draft.proposed_risk)
        suggestion = strictest_suggestion(suggestion, lens.draft.suggestion)
    return ReviewDraft(
        summary=_join_labelled(main.summary, [(lens.name, lens.draft.summary) for lens in lenses]),
        proposed_risk=risk,
        risk_rationale=_join_labelled(
            main.risk_rationale, [(lens.name, lens.draft.risk_rationale) for lens in lenses]
        ),
        suggestion=suggestion,
        suggestion_rationale=_join_labelled(
            main.suggestion_rationale,
            [(lens.name, lens.draft.suggestion_rationale) for lens in lenses],
        ),
        findings=findings,
        guidance_sources_used=_union(
            main.guidance_sources_used, *(lens.draft.guidance_sources_used for lens in lenses)
        ),
        related_sources_used=_union(
            main.related_sources_used, *(lens.draft.related_sources_used for lens in lenses)
        ),
        guidance_change_assessment=main.guidance_change_assessment,
        questions_for_author=_union(
            main.questions_for_author, *(lens.draft.questions_for_author for lens in lenses)
        ),
    )


def _add_finding(
    findings: list[Finding], candidate: Finding, *, main: Sequence[Finding], absorbed: set[int]
) -> None:
    # Each main finding absorbs at most one finding per lens, so a lens's own neighbours survive.
    for index, original in enumerate(main):
        if index not in absorbed and _same_issue(original, candidate):
            absorbed.add(index)
            if _strength(candidate) < _strength(findings[index]):
                findings[index] = candidate
            return
    findings.append(candidate)


def _same_issue(first: Finding, second: Finding) -> bool:
    # Passes label the same issue with different categories and anchor it a few lines apart.
    if (first.path, first.side) != (second.path, second.side):
        return False
    first_start = first.line if first.start_line is None else first.start_line
    second_start = second.line if second.start_line is None else second.start_line
    return (
        first_start - DUPLICATE_LINE_WINDOW <= second.line
        and second_start - DUPLICATE_LINE_WINDOW <= first.line
    )


def _strength(finding: Finding) -> tuple[int, int]:
    return (SEVERITY_RANK[finding.severity], CONFIDENCE_RANK[finding.confidence])


def _join_labelled(main: str, extras: Sequence[tuple[str, str]]) -> str:
    parts = [main]
    parts.extend(f"{name} pass: {text.strip()}" for name, text in extras if text.strip())
    return "\n\n".join(parts)


def _union(*groups: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item, None)
    return list(seen)
