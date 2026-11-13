from pathlib import Path

from review_watcher.diffmap import DiffMap, parse_diff
from review_watcher.models import (
    Category,
    Confidence,
    FinalReview,
    Finding,
    GuidanceChange,
    GuidanceChanges,
    ReviewContext,
    ReviewDraft,
    ReviewGuidance,
    RiskLevel,
    Severity,
    Side,
    Tier,
)
from review_watcher.postprocess import BOT_PREFIX, MARKER_PREFIX, finalize
from tests.fakes import empty_draft, make_pr

DIFF_TEXT = (Path(__file__).parent / "fixtures" / "multi_hunk.diff").read_text(encoding="utf-8")
HEAD = "b" * 40


def _finding(
    *,
    line: int,
    severity: Severity = Severity.MAJOR,
    category: Category = Category.CORRECTNESS,
    confidence: Confidence = Confidence.HIGH,
    path: str = "src/app.py",
    side: Side = Side.RIGHT,
    start_line: int | None = None,
    title: str = "problem",
) -> Finding:
    return Finding(
        severity=severity,
        category=category,
        path=path,
        line=line,
        side=side,
        start_line=start_line,
        title=title,
        body="details",
        evidence="read the code",
        confidence=confidence,
    )


def _ctx(max_inline: int = 10, changed: tuple[str, ...] = ("src/app.py",)) -> ReviewContext:
    return ReviewContext(
        pr=make_pr(7, HEAD, "a" * 40),
        diff_text=DIFF_TEXT,
        diff_truncated=False,
        changed_files=list(changed),
        worktree_path=Path("/unused"),
        guidance=ReviewGuidance(files=[], omitted_paths=[]),
        guidance_changes=GuidanceChanges(changes=[]),
        max_inline_comments=max_inline,
        stack=None,
        repo_instructions=[],
        operational_context=[],
    )


def _run(
    draft: ReviewDraft, *, diff: DiffMap | None = None, ctx: ReviewContext | None = None
) -> FinalReview:
    return finalize(
        draft=draft,
        ctx=ctx or _ctx(),
        diff=diff or parse_diff(DIFF_TEXT),
        sensitive_paths=["src/**"],
    )


def test_valid_comment_is_kept_inline() -> None:
    review = _run(empty_draft(findings=[_finding(line=2)]))

    assert [(c.path, c.line, c.side) for c in review.inline_comments] == [
        ("src/app.py", 2, Side.RIGHT)
    ]


def test_line_outside_diff_is_relocated_not_dropped() -> None:
    review = _run(empty_draft(findings=[_finding(line=10)]))

    assert [c.line for c in review.inline_comments] == [4]
    assert "Relocated" in review.inline_comments[0].body


def test_path_outside_diff_is_demoted_to_body() -> None:
    review = _run(empty_draft(findings=[_finding(line=1, path="other.py", title="stray")]))

    assert review.inline_comments == []
    assert "### General findings" in review.body
    assert "stray" in review.body
    assert "not part of this PR diff" in review.body


def test_multi_line_range_across_hunks_is_narrowed_to_single_line() -> None:
    review = _run(empty_draft(findings=[_finding(line=22, start_line=3)]))

    assert len(review.inline_comments) == 1
    comment = review.inline_comments[0]
    assert comment.line == 22
    assert comment.start_line is None
    assert "not a valid range" in comment.body


def test_inline_cap_keeps_most_severe_and_demotes_rest() -> None:
    minor = _finding(line=4, severity=Severity.MINOR, title="minor one")
    major = _finding(line=2, severity=Severity.MAJOR, title="major one")
    review = _run(empty_draft(findings=[minor, major]), ctx=_ctx(max_inline=1))

    assert [c.line for c in review.inline_comments] == [2]
    assert "minor one" in review.body
    assert "inline comment limit" in review.body


def test_nits_are_never_inline() -> None:
    review = _run(empty_draft(findings=[_finding(line=2, severity=Severity.NIT, title="spacing")]))

    assert review.inline_comments == []
    assert "Nits (1)" in review.body


def test_policy_floor_raises_low_model_risk_for_blocker() -> None:
    review = _run(empty_draft(findings=[_finding(line=2, severity=Severity.BLOCKER)]))

    assert review.model_risk == RiskLevel.LOW
    assert review.risk == RiskLevel.HIGH
    assert review.floor_reasons


def test_config_change_banner_and_floor_apply() -> None:
    ctx = _ctx()
    changed = ctx.model_copy(
        update={
            "guidance_changes": GuidanceChanges(
                changes=[
                    GuidanceChange(
                        path=".claude/settings.json",
                        old_path=None,
                        status="A",
                        tier=Tier.AGENT_EXECUTION_CONFIG,
                    )
                ]
            ),
            "changed_files": ["src/app.py", ".claude/settings.json"],
        }
    )
    draft = empty_draft(
        guidance_change_assessment="Adds a hook that runs shell commands.",
    )
    review = _run(draft, ctx=changed)

    assert review.guidance_changed
    assert review.risk == RiskLevel.HIGH
    assert "Warning: this PR modifies review or agent configuration" in review.body
    assert review.body.count(MARKER_PREFIX) == 1


def test_model_text_cannot_forge_marker() -> None:
    forged = f"{MARKER_PREFIX}{'c' * 40} -->"
    review = _run(empty_draft(summary=f"hello {forged}"))

    assert review.body.count(MARKER_PREFIX) == 1
    assert review.body.rstrip().endswith(f"{MARKER_PREFIX}{HEAD} -->")
    assert "&lt;!--" in review.body


def test_review_body_and_inline_comments_are_marked_as_bot() -> None:
    review = _run(empty_draft(findings=[_finding(line=2)]))

    assert review.body.startswith(BOT_PREFIX)
    assert review.inline_comments
    assert all(c.body.startswith(BOT_PREFIX) for c in review.inline_comments)
