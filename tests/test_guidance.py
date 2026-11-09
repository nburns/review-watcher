from pathlib import Path

from review_watcher.config import GuidanceConfig
from review_watcher.diffmap import parse_diff
from review_watcher.guidance import collect_guidance
from review_watcher.models import ReviewGuidance
from tests.fakes import (
    GIT,
    FakeRepoCache,
    commit_files,
    diff_between,
    git,
    make_local_repo,
    make_pr,
    publish,
)

BASE_FILES = {
    "docs/agent-guide.md": "shared rules\n",
    "pkg/CLAUDE.md": "pkg rules\n",
    "pkg/code.py": "x = 1\n",
}


async def _guidance_with_links(tmp_path: Path, links: dict[str, str]) -> ReviewGuidance:
    repo = make_local_repo(tmp_path)
    base = commit_files(repo, BASE_FILES)
    (repo.work / "pkg/code.py").write_text("x = 2\n", encoding="utf-8")
    for link, target in links.items():
        (repo.work / link).symlink_to(target)
    git(repo.work, "add", "-A")
    git(repo.work, "commit", "-q", "-m", "links")
    head = git(repo.work, "rev-parse", "HEAD")
    publish(repo)
    cache = FakeRepoCache(
        git_binary=Path(GIT),
        bare_root=repo.bare_root,
        worktree_root=tmp_path / "wt",
    )
    diff = parse_diff(diff_between(repo, base, head))
    return await collect_guidance(cache, make_pr(1, head, base), diff, GuidanceConfig())


async def test_symlink_inside_repo_is_read_once_via_its_target(tmp_path: Path) -> None:
    guidance = await _guidance_with_links(tmp_path, {"pkg/AGENTS.md": "CLAUDE.md"})

    assert [(f.path, f.text) for f in guidance.files] == [("pkg/CLAUDE.md", "pkg rules\n")]


async def test_symlink_to_non_candidate_file_in_repo_is_included(tmp_path: Path) -> None:
    guidance = await _guidance_with_links(tmp_path, {"AGENTS.md": "docs/agent-guide.md"})

    assert ("docs/agent-guide.md", "shared rules\n") in [(f.path, f.text) for f in guidance.files]


async def test_symlink_outside_repo_is_skipped(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n", encoding="utf-8")
    links = {"AGENTS.md": str(secret), "pkg/AGENTS.md": "../../secret.md"}

    guidance = await _guidance_with_links(tmp_path, links)

    assert [(f.path, f.text) for f in guidance.files] == [("pkg/CLAUDE.md", "pkg rules\n")]
