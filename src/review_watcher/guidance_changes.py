from collections.abc import Sequence
from pathlib import PurePosixPath

from review_watcher.config import GuidanceWatchConfig
from review_watcher.diffmap import DiffMap
from review_watcher.models import GuidanceChange, GuidanceChanges, PullRequestRef, Tier
from review_watcher.repo_cache import NameStatusEntry, RepoCache


class GuidanceMismatch(RuntimeError):
    pass


def _matches_any(path: str | None, patterns: Sequence[str]) -> bool:
    if path is None:
        return False
    posix = PurePosixPath(path)
    return any(posix.full_match(pattern) for pattern in patterns)


def _classify(entry: NameStatusEntry, watch: GuidanceWatchConfig) -> Tier | None:
    paths = (entry.path, entry.old_path)
    if any(_matches_any(path, watch.agent_execution_config) for path in paths):
        return Tier.AGENT_EXECUTION_CONFIG
    if any(_matches_any(path, watch.review_guidance) for path in paths):
        return Tier.REVIEW_GUIDANCE
    return None


async def collect_guidance_changes(
    repo: RepoCache,
    pr: PullRequestRef,
    diff: DiffMap,
    watch: GuidanceWatchConfig,
) -> GuidanceChanges:
    entries = await repo.diff_name_status(pr)
    git_paths = {entry.path for entry in entries}
    diff_paths = set(diff.changed_files)
    if git_paths != diff_paths:
        raise GuidanceMismatch(
            f"{pr.slug}: git diff paths and PR diff paths differ; "
            f"only_git={sorted(git_paths - diff_paths)} only_diff={sorted(diff_paths - git_paths)}"
        )

    changes: list[GuidanceChange] = []
    for entry in entries:
        tier = _classify(entry, watch)
        if tier is None:
            continue
        changes.append(
            GuidanceChange(path=entry.path, old_path=entry.old_path, status=entry.status, tier=tier)
        )
    return GuidanceChanges(changes=changes)
