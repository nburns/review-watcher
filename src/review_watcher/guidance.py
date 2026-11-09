import logging
import posixpath
from pathlib import PurePosixPath

from review_watcher.config import GuidanceConfig
from review_watcher.diffmap import DiffMap
from review_watcher.models import GuidanceFile, PullRequestRef, ReviewGuidance
from review_watcher.repo_cache import RepoCache, TreeEntry

_MAX_LINK_HOPS = 8

_ROOT_GUIDANCE_PATTERNS: tuple[str, ...] = (
    "CONTRIBUTING*",
    "REVIEWING*",
    ".github/PULL_REQUEST_TEMPLATE*",
    ".github/PULL_REQUEST_TEMPLATE/*",
    "docs/**/*review*",
)
_INSTRUCTION_FILE_NAMES: frozenset[str] = frozenset({"AGENTS.md", "CLAUDE.md"})

logger = logging.getLogger(__name__)


class UnresolvedLinkError(Exception):
    pass


def _parent_dir(posix: PurePosixPath) -> str:
    parent = str(posix.parent)
    return "" if parent == "." else parent


def _touched_dirs(changed_files: tuple[str, ...]) -> frozenset[str]:
    dirs: set[str] = {""}
    for path in changed_files:
        for parent in PurePosixPath(path).parents:
            dirs.add("" if str(parent) == "." else str(parent))
    return frozenset(dirs)


def _is_candidate(path: str, touched_dirs: frozenset[str], extra_paths: list[str]) -> bool:
    posix = PurePosixPath(path)
    if path.startswith(".claude/skills/") and posix.suffix == ".md":
        return True
    if path.startswith(".claude/commands/") and "review" in posix.stem.lower():
        return True
    if posix.name in _INSTRUCTION_FILE_NAMES and _parent_dir(posix) in touched_dirs:
        return True
    if any(posix.full_match(pattern) for pattern in _ROOT_GUIDANCE_PATTERNS):
        return True
    return any(posix.full_match(pattern) for pattern in extra_paths)


async def _resolve_link(
    repo: RepoCache,
    pr: PullRequestRef,
    entry: TreeEntry,
    by_path: dict[str, TreeEntry],
) -> TreeEntry:
    current = entry
    for _ in range(_MAX_LINK_HOPS):
        if current.is_blob:
            return current
        if not current.is_symlink:
            raise UnresolvedLinkError(f"{current.path} is a submodule")
        target = await repo.read_symlink_target(pr, current)
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(current.path), target))
        if posixpath.isabs(target) or joined == ".." or joined.startswith("../"):
            raise UnresolvedLinkError(f"{current.path} points outside the repo")
        # Only tree entries are read, so links through symlinked dirs are skipped, not followed.
        if joined not in by_path:
            raise UnresolvedLinkError(f"{current.path} target {joined} is not a file in the tree")
        current = by_path[joined]
    raise UnresolvedLinkError(f"{entry.path} exceeds {_MAX_LINK_HOPS} symlink hops")


async def collect_guidance(
    repo: RepoCache,
    pr: PullRequestRef,
    diff: DiffMap,
    config: GuidanceConfig,
) -> ReviewGuidance:
    changed = diff.changed_files
    touched = _touched_dirs(changed)
    tree = await repo.list_tree(pr)
    candidates = sorted(
        (entry for entry in tree if _is_candidate(entry.path, touched, config.extra_paths)),
        key=lambda entry: entry.path,
    )

    by_path = {entry.path: entry for entry in tree}
    resolved: dict[str, TreeEntry] = {}
    for candidate in candidates:
        try:
            entry = await _resolve_link(repo, pr, candidate, by_path)
        except UnresolvedLinkError as exc:
            logger.info("guidance_skipped_link pr=%s reason=%s", pr.slug, exc)
            continue
        resolved.setdefault(entry.path, entry)

    files: list[GuidanceFile] = []
    omitted: list[str] = []
    remaining = config.max_total_bytes
    for entry in resolved.values():
        if remaining <= 0:
            omitted.append(entry.path)
            continue
        raw = await repo.read_blob(pr, entry)
        budget = min(config.max_file_bytes, remaining)
        clipped = raw[:budget]
        remaining -= len(clipped)
        modified = entry.path in changed
        files.append(
            GuidanceFile(
                path=entry.path,
                text=clipped.decode("utf-8", errors="replace"),
                truncated=len(raw) > budget,
                modified_in_pr=modified,
                pr_diff=diff.files[entry.path].text if modified else None,
            )
        )
    return ReviewGuidance(files=files, omitted_paths=omitted)
