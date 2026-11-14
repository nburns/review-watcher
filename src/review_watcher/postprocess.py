from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from review_watcher.diffmap import DiffMap
from review_watcher.models import (
    CONFIDENCE_RANK,
    SEVERITY_RANK,
    Category,
    Confidence,
    ContextResult,
    FinalReview,
    Finding,
    GuidanceChange,
    GuidanceChanges,
    InlineComment,
    PullRequestRef,
    RelatedContext,
    ReviewContext,
    ReviewDraft,
    RiskLevel,
    Severity,
    Side,
    StackContext,
    Tier,
    max_risk,
)

MARKER_PREFIX = "<!-- review-watcher sha="
BOT_PREFIX = "🤖:"
RELOCATE_WINDOW = 20


@dataclass(frozen=True)
class _Placement:
    line: int | None
    start_line: int | None
    side: Side
    note: str | None


@dataclass(frozen=True)
class _Routed:
    finding: Finding
    placement: _Placement


def finalize(
    *,
    draft: ReviewDraft,
    ctx: ReviewContext,
    diff: DiffMap,
    sensitive_paths: Sequence[str],
) -> FinalReview:
    routed = [_Routed(finding, _place(finding, diff)) for finding in draft.findings]
    inline_candidates = [
        item
        for item in routed
        if item.placement.line is not None and item.finding.severity != Severity.NIT
    ]
    ranked = sorted(
        inline_candidates,
        key=lambda item: (
            SEVERITY_RANK[item.finding.severity],
            CONFIDENCE_RANK[item.finding.confidence],
        ),
    )
    inline_keep = {id(item) for item in ranked[: ctx.max_inline_comments]}

    inline_comments: list[InlineComment] = []
    general: list[_Routed] = []
    nits: list[_Routed] = []
    for item in routed:
        if item.finding.severity == Severity.NIT:
            nits.append(item)
        elif id(item) in inline_keep:
            inline_comments.append(_to_inline(item))
        else:
            general.append(item)

    model_risk = draft.proposed_risk
    floor, reasons = _risk_floor(draft.findings, ctx, sensitive_paths)
    final_risk = max_risk(model_risk, floor)
    body = _render_body(
        draft=draft,
        ctx=ctx,
        final_risk=final_risk,
        model_risk=model_risk,
        floor_reasons=reasons,
        general=general,
        nits=nits,
        inline_limit_hit=len(inline_candidates) > len(inline_keep),
    )
    return FinalReview(
        pr=ctx.pr,
        risk=final_risk,
        model_risk=model_risk,
        floor_reasons=reasons,
        body=body,
        inline_comments=inline_comments,
        guidance_changes=list(ctx.guidance_changes.changes),
    )


def fold_inline_into_body(review: FinalReview) -> FinalReview:
    if not review.inline_comments:
        return review
    lines = ["", "### Inline comments that could not be attached", ""]
    for comment in review.inline_comments:
        location = f"{comment.path}:{comment.line} ({comment.side.value})"
        lines.append(f"- `{location}`")
        lines.append("")
        lines.append(_indent_quote(comment.body))
        lines.append("")
    folded_section = "\n".join(lines)
    marker_at = review.body.rfind(MARKER_PREFIX)
    if marker_at == -1:
        raise RuntimeError("review body is missing its marker")
    head = review.body[:marker_at].rstrip()
    new_body = f"{head}\n{folded_section}\n{review.body[marker_at:]}"
    return review.model_copy(update={"body": new_body, "inline_comments": []})


def _place(finding: Finding, diff: DiffMap) -> _Placement:
    file_diff = diff.files.get(finding.path)
    if file_diff is None:
        return _Placement(None, None, finding.side, "path is not part of this PR diff")
    if diff.has_position(finding.path, finding.side, finding.line):
        return _keep_or_narrow(finding, diff)
    nearest = _nearest_position(diff, finding.path, finding.side, finding.line)
    if nearest is None:
        return _Placement(
            None,
            None,
            finding.side,
            f"line {finding.line} ({finding.side.value}) is not part of the diff",
        )
    note = (
        f"Relocated from {finding.side.value} line {finding.line} "
        f"to the nearest changed line {nearest}."
    )
    return _Placement(nearest, None, finding.side, note)


def _keep_or_narrow(finding: Finding, diff: DiffMap) -> _Placement:
    if finding.start_line is None:
        return _Placement(finding.line, None, finding.side, None)
    start = finding.start_line
    same_hunk = diff.hunk_for(finding.path, finding.side, start) == diff.hunk_for(
        finding.path, finding.side, finding.line
    )
    if same_hunk and start <= finding.line and diff.has_position(finding.path, finding.side, start):
        return _Placement(finding.line, start, finding.side, None)
    note = (
        f"The requested range starting at line {start} is not a valid range within one "
        f"diff hunk; the comment is placed on line {finding.line}."
    )
    return _Placement(finding.line, None, finding.side, note)


def _nearest_position(diff: DiffMap, path: str, side: Side, line: int) -> int | None:
    file_diff = diff.files[path]
    best: int | None = None
    best_distance = RELOCATE_WINDOW + 1
    for hunk in file_diff.hunks:
        for position in hunk.positions:
            if position.side != side:
                continue
            distance = abs(position.line - line)
            if distance < best_distance:
                best, best_distance = position.line, distance
    if best is None or best_distance > RELOCATE_WINDOW:
        return None
    return best


def _to_inline(item: _Routed) -> InlineComment:
    finding = item.finding
    placement = item.placement
    if placement.line is None:
        raise RuntimeError("inline comment requires a placed line")
    body = _finding_body(finding, placement.note)
    return InlineComment(
        path=finding.path,
        line=placement.line,
        side=placement.side,
        start_line=placement.start_line,
        body=body,
    )


def _finding_body(finding: Finding, note: str | None) -> str:
    prefix = ""
    if finding.confidence == Confidence.LOW:
        prefix = "Possible issue (low confidence): "
    parts = [
        f"{BOT_PREFIX} **{finding.severity.value.upper()} | {finding.category.value}: "
        f"{_neutralize(finding.title)}**",
        f"{prefix}{_neutralize(finding.body)}",
        f"Evidence: {_neutralize(finding.evidence)}",
    ]
    if note is not None:
        parts.append(f"_{note}_")
    return "\n\n".join(parts)


def _risk_floor(
    findings: Sequence[Finding],
    ctx: ReviewContext,
    sensitive_paths: Sequence[str],
) -> tuple[RiskLevel, list[str]]:
    level = RiskLevel.LOW
    reasons: list[str] = []
    if any(f.severity == Severity.BLOCKER for f in findings):
        level = max_risk(level, RiskLevel.HIGH)
        reasons.append("a blocker finding is present")
    if any(
        f.category == Category.SECURITY and f.severity in (Severity.BLOCKER, Severity.MAJOR)
        for f in findings
    ):
        level = max_risk(level, RiskLevel.HIGH)
        reasons.append("a security finding is major or above")
    if sum(1 for f in findings if f.severity == Severity.MAJOR) >= 3:
        level = max_risk(level, RiskLevel.MEDIUM)
        reasons.append("three or more major findings")
    if _touches_sensitive(ctx.changed_files, sensitive_paths):
        level = max_risk(level, RiskLevel.MEDIUM)
        reasons.append("a sensitive path is changed")
    for change in ctx.guidance_changes.changes:
        required = _tier_floor(change)
        level = max_risk(level, required)
        reasons.append(f"review configuration changed ({change.tier.value}: {change.path})")
    return level, reasons


def _tier_floor(change: GuidanceChange) -> RiskLevel:
    if change.tier == Tier.AGENT_EXECUTION_CONFIG:
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


def _touches_sensitive(changed_files: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(
        PurePosixPath(path).full_match(pattern) for path in changed_files for pattern in patterns
    )


def _render_body(
    *,
    draft: ReviewDraft,
    ctx: ReviewContext,
    final_risk: RiskLevel,
    model_risk: RiskLevel,
    floor_reasons: list[str],
    general: list[_Routed],
    nits: list[_Routed],
    inline_limit_hit: bool,
) -> str:
    sections: list[str] = [BOT_PREFIX]
    if ctx.guidance_changes.changes:
        sections.append(_guidance_banner(ctx.pr, ctx.guidance_changes, draft))
    if ctx.stack is not None:
        sections.append(_stack_note(ctx.pr, ctx.stack))
    if ctx.operational_context:
        sections.append(_context_note(ctx.operational_context))
    if ctx.related is not None:
        sections.append(_related_note(ctx.related))
    sections.append(_risk_section(final_risk, model_risk, floor_reasons, draft))
    sections.append(_severity_table(draft.findings))
    sections.append("### Summary\n\n" + _neutralize(draft.summary))
    if ctx.diff_truncated:
        sections.append(
            "> The diff exceeded the size limit and was truncated, so this change was "
            "**not fully assessed**."
        )
    if general or inline_limit_hit:
        sections.append(_general_section(general, inline_limit_hit))
    if draft.questions_for_author:
        questions = "\n".join(f"- {_neutralize(q)}" for q in draft.questions_for_author)
        sections.append("### Questions for the author\n\n" + questions)
    if nits:
        sections.append(_nits_section(nits))
    if draft.guidance_sources_used:
        sources = ", ".join(_neutralize(s) for s in draft.guidance_sources_used)
        sections.append(f"Guidance sources used: {sources}")
    if draft.related_sources_used:
        sources = ", ".join(f"`{_cell(s)}`" for s in draft.related_sources_used)
        sections.append(f"Related sources used: {sources}")
    sections.append(f"{MARKER_PREFIX}{ctx.pr.head_sha} -->")
    return "\n\n".join(sections)


def _context_note(results: Sequence[ContextResult]) -> str:
    items: list[str] = []
    for result in results:
        if result.error is not None:
            items.append(f"`{_cell(result.name)}` (**unavailable**: {_cell(result.error)})")
        elif result.truncated:
            items.append(f"`{_cell(result.name)}` (truncated)")
        else:
            items.append(f"`{_cell(result.name)}`")
    return "> Operational context: " + ", ".join(items)


def _related_note(related: RelatedContext) -> str:
    items: list[str] = []
    for repo in related.repos:
        if repo.checkout is None:
            items.append(f"`{_cell(repo.full_name)}` (**unavailable**: {_cell(repo.error or '')})")
        else:
            items.append(f"`{_cell(repo.full_name)}@{repo.checkout.commit_sha[:12]}`")
    items.extend(
        f"{_cell(linked.full_name)}#{linked.number} ({linked.state})"
        for linked in related.linked_prs
    )
    items.extend(
        f"{_cell(failure.reference)} (**unavailable**: {_cell(failure.error)})"
        for failure in related.link_failures
    )
    return "> Related context: " + ", ".join(items)


def _stack_note(pr: PullRequestRef, stack: StackContext) -> str:
    below = ", ".join(f"#{ancestor.number}" for ancestor in stack.ancestors)
    on_top = f" on top of {below}" if below else ""
    lines = [
        f"> Stacked PR: merges into `{_cell(pr.base_ref)}`{on_top}, not "
        f"`{_cell(stack.default_branch)}`. Reviewed with the full "
        f"`{_cell(stack.default_branch)}...head` diff as context; findings cover only this "
        "PR's changes."
    ]
    if stack.diff_truncated:
        lines.append(
            "> The stack diff exceeded the size limit and was truncated, so interactions with "
            "lower PRs were **not fully assessed**."
        )
    return "\n".join(lines)


def _guidance_banner(pr: PullRequestRef, changes: GuidanceChanges, draft: ReviewDraft) -> str:
    rows = ["| File | Change | Tier | Link |", "| --- | --- | --- | --- |"]
    for change in changes.changes:
        name = change.path if change.old_path is None else f"{change.old_path} -> {change.path}"
        rows.append(
            f"| {_cell(name)} | {change.status} | {change.tier.value} "
            f"| [Files changed]({pr.url}/files) |"
        )
    assessment = draft.guidance_change_assessment or "(no assessment provided)"
    return "\n".join(
        [
            "> **Warning: this PR modifies review or agent configuration.**",
            "> This review used the PR-head versions of these files.",
            "",
            *rows,
            "",
            f"**Assessment:** {_neutralize(assessment)}",
        ]
    )


def _risk_section(
    final_risk: RiskLevel,
    model_risk: RiskLevel,
    floor_reasons: list[str],
    draft: ReviewDraft,
) -> str:
    header = f"### Risk: {final_risk.value.upper()}"
    if final_risk != model_risk:
        header += f" (model proposed {model_risk.value}, raised by policy)"
    lines = [header, ""]
    if floor_reasons:
        lines.extend(f"- Policy floor: {reason}" for reason in floor_reasons)
        lines.append("")
    lines.append(_neutralize(draft.risk_rationale))
    return "\n".join(lines)


def _severity_table(findings: Sequence[Finding]) -> str:
    rows = ["| Severity | Count |", "| --- | --- |"]
    for severity in Severity:
        count = sum(1 for finding in findings if finding.severity == severity)
        rows.append(f"| {severity.value} | {count} |")
    return "\n".join(rows)


def _general_section(general: list[_Routed], inline_limit_hit: bool) -> str:
    lines = ["### General findings", ""]
    for item in general:
        finding = item.finding
        location = f"`{finding.path}:{finding.line}`"
        text = f"- **{finding.severity.value}** {location} {_neutralize(finding.title)}"
        text += f": {_neutralize(finding.body)} Evidence: {_neutralize(finding.evidence)}"
        if item.placement.note is not None:
            text += f" _{item.placement.note}_"
        lines.append(text)
    if inline_limit_hit:
        lines.append("")
        lines.append("_The inline comment limit was reached; remaining findings are listed here._")
    return "\n".join(lines)


def _nits_section(nits: list[_Routed]) -> str:
    items = "\n".join(
        f"- `{item.finding.path}:{item.finding.line}` {_neutralize(item.finding.title)}: "
        f"{_neutralize(item.finding.body)}"
        for item in nits
    )
    return f"<details><summary>Nits ({len(nits)})</summary>\n\n{items}\n\n</details>"


def _neutralize(text: str) -> str:
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _cell(text: str) -> str:
    return _neutralize(text).replace("|", "\\|").replace("\n", " ")


def _indent_quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())
