from pathlib import Path

import pytest

from review_watcher.config import GuidanceWatchConfig
from review_watcher.diffmap import parse_diff
from review_watcher.guidance_changes import GuidanceMismatch, collect_guidance_changes
from review_watcher.models import Tier
from tests.fakes import (
    GIT,
    FakeRepoCache,
    commit_files,
    diff_between,
    make_local_repo,
    make_pr,
    publish,
)

BASE_FILES = {
    "README.md": "readme\n",
    "AGENTS.md": "root rules\n",
    "pkg/AGENTS.md": "pkg rules\n",
    "pkg/code.py": "x = 1\n",
    ".claude/skills/review/SKILL.md": "skill\n",
    "CLAUDE.md": "claude rules\n",
}


async def _changes_for(
    tmp_path: Path, head_files: dict[str, str]
) -> list[tuple[str, str | None, Tier]]:
    repo = make_local_repo(tmp_path)
    base = commit_files(repo, BASE_FILES)
    head = commit_files(repo, head_files)
    publish(repo)
    pr = make_pr(1, head, base)
    cache = FakeRepoCache(
        git_binary=Path(GIT),
        bare_root=repo.bare_root,
        worktree_root=tmp_path / "wt",
        related_root=tmp_path / "related",
    )
    diff = parse_diff(diff_between(repo, base, head))
    result = await collect_guidance_changes(cache, pr, diff, GuidanceWatchConfig())
    return [(c.path, c.old_path, c.tier) for c in result.changes]


async def test_nested_instruction_file_and_settings_are_classified(tmp_path: Path) -> None:
    head_files = {**BASE_FILES, "pkg/AGENTS.md": "weakened\n", ".claude/settings.json": "{}\n"}
    changes = await _changes_for(tmp_path, head_files)

    assert ("pkg/AGENTS.md", None, Tier.REVIEW_GUIDANCE) in changes
    assert (".claude/settings.json", None, Tier.AGENT_EXECUTION_CONFIG) in changes


async def test_skill_moved_out_of_claude_dir_is_caught_via_old_path(tmp_path: Path) -> None:
    head_files = {k: v for k, v in BASE_FILES.items() if not k.startswith(".claude/skills")}
    head_files["skills/review/SKILL.md"] = "skill\n"
    changes = await _changes_for(tmp_path, head_files)

    assert (
        "skills/review/SKILL.md",
        ".claude/skills/review/SKILL.md",
        Tier.REVIEW_GUIDANCE,
    ) in changes


async def test_deleted_claude_md_is_caught(tmp_path: Path) -> None:
    head_files = {k: v for k, v in BASE_FILES.items() if k != "CLAUDE.md"}
    changes = await _changes_for(tmp_path, head_files)

    assert ("CLAUDE.md", None, Tier.REVIEW_GUIDANCE) in changes


async def test_unrelated_change_yields_empty_list(tmp_path: Path) -> None:
    head_files = {**BASE_FILES, "pkg/code.py": "x = 2\n"}
    assert await _changes_for(tmp_path, head_files) == []


async def test_mismatch_between_git_and_pr_diff_raises(tmp_path: Path) -> None:
    repo = make_local_repo(tmp_path)
    base = commit_files(repo, BASE_FILES)
    head = commit_files(repo, {**BASE_FILES, "pkg/code.py": "x = 2\n"})
    publish(repo)
    cache = FakeRepoCache(
        git_binary=Path(GIT),
        bare_root=repo.bare_root,
        worktree_root=tmp_path / "wt",
        related_root=tmp_path / "related",
    )
    unrelated = parse_diff("")
    with pytest.raises(GuidanceMismatch):
        await collect_guidance_changes(
            cache, make_pr(1, head, base), unrelated, GuidanceWatchConfig()
        )
