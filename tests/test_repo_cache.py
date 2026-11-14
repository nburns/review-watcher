import shutil
from dataclasses import dataclass
from pathlib import Path

from tests.fakes import (
    GIT,
    FakeRepoCache,
    LocalRepo,
    commit_files,
    git,
    make_local_repo,
    make_pr,
    publish,
)


@dataclass(frozen=True)
class _Stale:
    repo: LocalRepo
    cache: FakeRepoCache
    head: str


def _stale_locked_worktree(tmp_path: Path) -> _Stale:
    repo = make_local_repo(tmp_path)
    head = commit_files(repo, {"README.md": "readme\n"})
    publish(repo)
    worktree_root = tmp_path / "wt"
    cache = FakeRepoCache(
        git_binary=Path(GIT),
        bare_root=repo.bare_root,
        worktree_root=worktree_root,
        related_root=tmp_path / "related",
    )
    stale = worktree_root / f"acme-app-1-{head[:12]}"
    git(
        repo.bare,
        "worktree",
        "add",
        "--detach",
        "--lock",
        "--reason",
        "initializing",
        str(stale),
        head,
    )
    shutil.rmtree(stale)
    return _Stale(repo=repo, cache=cache, head=head)


async def test_prune_clears_worktree_left_locked_by_killed_add(tmp_path: Path) -> None:
    stale = _stale_locked_worktree(tmp_path)

    await stale.cache.prune()

    assert "locked" not in git(stale.repo.bare, "worktree", "list", "--porcelain")


async def test_worktree_add_overrides_stale_lock(tmp_path: Path) -> None:
    stale = _stale_locked_worktree(tmp_path)

    async with stale.cache.worktree(make_pr(1, stale.head, stale.head)) as path:
        assert (path / "README.md").read_text() == "readme\n"
