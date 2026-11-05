from pathlib import Path

from review_watcher.diffmap import parse_diff
from review_watcher.models import Side

FIXTURES = Path(__file__).parent / "fixtures"


def test_commentable_positions_follow_hunks() -> None:
    diff = parse_diff((FIXTURES / "multi_hunk.diff").read_text(encoding="utf-8"))

    assert diff.changed_files == ("src/app.py",)
    assert diff.has_position("src/app.py", Side.RIGHT, 2)
    assert diff.has_position("src/app.py", Side.RIGHT, 22)
    assert diff.has_position("src/app.py", Side.LEFT, 2)
    assert not diff.has_position("src/app.py", Side.RIGHT, 10)
    assert not diff.has_position("src/app.py", Side.RIGHT, 100)
    assert not diff.has_position("missing.py", Side.RIGHT, 1)


def test_renames_and_deletions_use_expected_paths() -> None:
    diff = parse_diff((FIXTURES / "rename_delete.diff").read_text(encoding="utf-8"))

    assert diff.changed_files == ("new/name.md", "gone.txt")
    assert diff.has_position("gone.txt", Side.LEFT, 1)
    assert not diff.has_position("gone.txt", Side.RIGHT, 1)
