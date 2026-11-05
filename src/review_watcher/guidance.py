from pathlib import PurePosixPath

from review_watcher.config import GuidanceConfig
from review_watcher.diffmap import DiffMap
from review_watcher.models import GuidanceFile, PullRequestRef, ReviewGuidance
from review_watcher.repo_cache import RepoCache

_ROOT_GUIDANCE_PATTERNS: tuple[str, ...] = (
    "CONTRIBUTING*",
    "REVIEWING*",
    ".github/PULL_REQUEST_TEMPLATE*",
    ".github/PULL_REQUEST_TEMPLATE/*",
    "docs/**/*review*",
)
_INSTRUCTION_FILE_NAMES: frozenset[str] = frozenset({"AGENTS.md", "CLAUDE.md"})


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

    files: list[GuidanceFile] = []
    omitted: list[str] = []
    remaining = config.max_total_bytes
    for entry in candidates:
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
