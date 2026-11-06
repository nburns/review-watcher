import json
from pathlib import Path

import pytest

from review_watcher.models import (
    GuidanceChange,
    GuidanceChanges,
    ReviewContext,
    ReviewGuidance,
    Tier,
)
from review_watcher.reviewer import ReviewOutputInvalid, parse_review_output
from tests.fakes import empty_draft, make_pr


def _ctx(*, with_changes: bool) -> ReviewContext:
    changes: list[GuidanceChange] = []
    if with_changes:
        changes.append(
            GuidanceChange(path="AGENTS.md", old_path=None, status="M", tier=Tier.REVIEW_GUIDANCE)
        )
    return ReviewContext(
        pr=make_pr(3, "d" * 40, "e" * 40),
        diff_text="",
        diff_truncated=False,
        changed_files=[],
        worktree_path=Path("/unused"),
        guidance=ReviewGuidance(files=[], omitted_paths=[]),
        guidance_changes=GuidanceChanges(changes=changes),
        max_inline_comments=10,
        stack=None,
    )


def _envelope(structured: object, *, is_error: bool = False) -> bytes:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "structured_output": structured,
        }
    ).encode()


def test_valid_structured_output_is_parsed() -> None:
    draft = parse_review_output(
        _envelope(empty_draft().model_dump(mode="json")), _ctx(with_changes=False)
    )

    assert draft.proposed_risk == "low"
    assert draft.findings == []


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json at all",
        json.dumps({"is_error": True, "subtype": "error_max_turns"}).encode(),
        json.dumps({"is_error": False, "subtype": "success"}).encode(),
        _envelope(None),
        _envelope({"summary": "missing most fields"}),
    ],
)
def test_malformed_output_raises(stdout: bytes) -> None:
    with pytest.raises(ReviewOutputInvalid):
        parse_review_output(stdout, _ctx(with_changes=False))


def test_missing_assessment_raises_when_guidance_changed() -> None:
    structured = empty_draft().model_dump(mode="json")
    assert structured["guidance_change_assessment"] is None

    with pytest.raises(ReviewOutputInvalid, match="guidance_change_assessment"):
        parse_review_output(_envelope(structured), _ctx(with_changes=True))
