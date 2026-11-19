from pathlib import Path

import pytest

from review_watcher.lenses import has_runtime_change, load_lens_pass
from review_watcher.merge import LensDraft, merge_drafts
from tests.fakes import empty_draft


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["README.md", "docs/setup.rst", "tests/test_app.py"], False),
        (["web/src/__tests__/a.ts", "web/src/a.test.ts", "pkg/a_test.go"], False),
        ([], False),
        (["README.md", "src/app.py"], True),
        (["requirements.txt"], True),
        (["infra/main.tf"], True),
    ],
)
def test_runtime_change_ignores_docs_and_tests(paths: list[str], expected: bool) -> None:
    assert has_runtime_change(paths) is expected


def test_sre_lens_pass_indexes_every_vendored_skill() -> None:
    lens = load_lens_pass("sre")

    assert len(lens.skills) >= 10
    for skill in lens.skills:
        assert skill.name is not None
        assert skill.description is not None
        assert (Path(skill.path) / "SKILL.md").is_file()
        assert Path(skill.path).parent in lens.skill_dirs


def _finding(category: str, line: int, severity: str, title: str) -> dict[str, object]:
    return {
        "severity": severity,
        "category": category,
        "path": "src/app.py",
        "line": line,
        "side": "RIGHT",
        "start_line": None,
        "title": title,
        "body": "b",
        "evidence": "e",
        "quote": None,
        "confidence": "high",
    }


def test_merge_keeps_the_stronger_of_overlapping_findings() -> None:
    main = empty_draft(
        findings=[
            _finding("reliability", 3, "minor", "main weak"),
            _finding("correctness", 3, "major", "main other category"),
        ],
        questions_for_author=["Why?"],
    )
    lens = empty_draft(
        summary="Ops view.",
        proposed_risk="high",
        findings=[
            _finding("reliability", 3, "blocker", "lens strong"),
            _finding("reliability", 9, "minor", "lens separate"),
        ],
        questions_for_author=["Why?", "Rollback?"],
    )

    merged = merge_drafts(main, [LensDraft(name="sre", draft=lens)])

    assert [f.title for f in merged.findings] == [
        "lens strong",
        "main other category",
        "lens separate",
    ]
    assert merged.proposed_risk == "high"
    assert merged.summary == "Looks fine.\n\nsre pass: Ops view."
    assert merged.questions_for_author == ["Why?", "Rollback?"]
