import asyncio
import logging
import shutil
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from review_watcher.logging_setup import log_step
from review_watcher.models import PullRequestRef
from review_watcher.proc import CommandResult, base_env, run_command

GIT_TIMEOUT_S = 600.0
_GIT_HARDENING: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "submodule.recurse=false",
)
_GIT_ENV_EXTRA: Mapping[str, str] = {"GIT_LFS_SKIP_SMUDGE": "1"}
_SYMLINK_MODE = "120000"
_SUBMODULE_MODE = "160000"


logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str

    @property
    def is_blob(self) -> bool:
        return self.mode not in (_SYMLINK_MODE, _SUBMODULE_MODE)

    @property
    def is_symlink(self) -> bool:
        return self.mode == _SYMLINK_MODE


@dataclass(frozen=True)
class CheckoutState:
    path: Path
    commit_sha: str
    committed_at: datetime


@dataclass(frozen=True)
class NameStatusEntry:
    status: str
    path: str
    old_path: str | None


class RepoCache:
    def __init__(
        self, *, git_binary: Path, bare_root: Path, worktree_root: Path, related_root: Path
    ) -> None:
        self._git_bin = str(git_binary)
        self._bare_root = bare_root
        self._worktree_root = worktree_root
        self._related_root = related_root
        # git reports worktree paths with symlinks resolved, e.g. /var -> /private/var.
        self._worktree_root_real = worktree_root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self._env = base_env(_GIT_ENV_EXTRA)

    def bare_path(self, pr: PullRequestRef) -> Path:
        return self._bare_for(pr.full_name)

    def _bare_for(self, full_name: str) -> Path:
        owner, repo = full_name.split("/")
        return self._bare_root / owner / f"{repo}.git"

    def related_path(self, full_name: str) -> Path:
        owner, repo = full_name.lower().split("/")
        return self._related_root / owner / repo

    def related_stamp_path(self, full_name: str) -> Path:
        path = self.related_path(full_name)
        return path.with_name(f"{path.name}.refreshed.json")

    def _lock_for(self, pr: PullRequestRef) -> asyncio.Lock:
        return self._repo_lock(pr.full_name)

    def _repo_lock(self, full_name: str) -> asyncio.Lock:
        # GitHub names are case-insensitive; related repos use lowercased config names.
        return self._locks.setdefault(full_name.lower(), asyncio.Lock())

    async def _git(self, args: list[str], *, cwd: Path | None = None) -> CommandResult:
        return await run_command(
            [self._git_bin, *_GIT_HARDENING, *args],
            env=self._env,
            timeout_s=GIT_TIMEOUT_S,
            cwd=cwd,
        )

    async def _git_bare(self, pr: PullRequestRef, args: list[str]) -> CommandResult:
        return await self._git(["-C", str(self.bare_path(pr)), *args])

    async def _ensure_clone(self, full_name: str) -> Path:
        bare = self._bare_for(full_name)
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            await self._git(
                [
                    "clone",
                    "--bare",
                    "--filter=blob:none",
                    "--",
                    f"git@github.com:{full_name}.git",
                    str(bare),
                ]
            )
        return bare

    async def ensure_fetched(self, pr: PullRequestRef) -> None:
        async with self._lock_for(pr):
            await self._ensure_clone(pr.full_name)
            refspecs = [
                f"+refs/pull/{pr.number}/head:refs/rw/pr/{pr.number}",
                f"+refs/heads/{pr.base_ref}:refs/rw/base/{pr.base_ref}",
            ]
            if pr.is_stacked:
                refspecs.append(f"+refs/heads/{pr.default_branch}:refs/rw/base/{pr.default_branch}")
            await self._git_bare(pr, ["fetch", "--filter=blob:none", "origin", *refspecs])
            for sha in (pr.head_sha, pr.base_sha):
                await self._git_bare(pr, ["cat-file", "-e", f"{sha}^{{commit}}"])

    @asynccontextmanager
    async def worktree(self, pr: PullRequestRef) -> AsyncGenerator[Path]:
        with log_step(logger, pr.slug, "fetch"):
            await self.ensure_fetched(pr)
        path = self._worktree_root / f"{pr.owner}-{pr.repo}-{pr.number}-{pr.head_sha[:12]}"
        with log_step(logger, pr.slug, "worktree_add"):
            async with self._lock_for(pr):
                # Double force overrides a stale locked registration left by a killed add.
                await self._git_bare(
                    pr,
                    ["worktree", "add", "--force", "--force", "--detach", str(path), pr.head_sha],
                )
        try:
            yield path
        finally:
            await self._git_bare(pr, ["worktree", "remove", "--force", str(path)])

    async def list_tree(self, pr: PullRequestRef) -> list[TreeEntry]:
        result = await self._git_bare(pr, ["ls-tree", "-r", "-z", "--full-tree", pr.head_sha])
        return _parse_tree(result.stdout)

    async def read_blob(self, pr: PullRequestRef, entry: TreeEntry) -> bytes:
        return await self._read_blob_in(self.bare_path(pr), pr.head_sha, entry)

    async def _read_blob_in(self, bare: Path, sha: str, entry: TreeEntry) -> bytes:
        if not entry.is_blob:
            raise GitError(f"refusing to read non-blob entry {entry.path} (mode {entry.mode})")
        result = await self._git(["-C", str(bare), "cat-file", "blob", f"{sha}:{entry.path}"])
        return result.stdout

    async def refresh_related(self, full_name: str, branch: str) -> CheckoutState:
        path = self.related_path(full_name)
        async with self._repo_lock(full_name):
            bare = await self._ensure_clone(full_name)
            ref = await self._fetch_branch(bare, branch)
            sha = await self._rev_parse(bare, ref)
            if (path / ".git").is_file():
                await self._git(["-C", str(path), "checkout", "--force", "--detach", sha])
            else:
                if path.exists():
                    shutil.rmtree(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                await self._git(["-C", str(bare), "worktree", "prune"])
                await self._git(
                    [
                        "-C",
                        str(bare),
                        "worktree",
                        "add",
                        "--force",
                        "--force",
                        "--detach",
                        str(path),
                        sha,
                    ]
                )
            return await self._checkout_state(bare, path, sha)

    async def _fetch_branch(self, bare: Path, branch: str) -> str:
        ref = f"refs/rw/base/{branch}"
        await self._git(
            [
                "-C",
                str(bare),
                "fetch",
                "--filter=blob:none",
                "origin",
                f"+refs/heads/{branch}:{ref}",
            ]
        )
        return ref

    async def current_related(self, full_name: str) -> CheckoutState:
        path = self.related_path(full_name)
        if not (path / ".git").is_file():
            raise GitError(f"no related checkout for {full_name} at {path}")
        sha = await self._rev_parse(path, "HEAD")
        return await self._checkout_state(self._bare_for(full_name), path, sha)

    async def list_related_root(self, full_name: str, sha: str) -> list[TreeEntry]:
        result = await self._git(["-C", str(self._bare_for(full_name)), "ls-tree", "-z", sha])
        return _parse_tree(result.stdout)

    async def read_related_blob(self, full_name: str, sha: str, entry: TreeEntry) -> bytes:
        return await self._read_blob_in(self._bare_for(full_name), sha, entry)

    async def read_path_at(self, full_name: str, sha: str, path: str) -> bytes:
        result = await self._git(
            ["-C", str(self._bare_for(full_name)), "cat-file", "blob", f"{sha}:{path}"]
        )
        return result.stdout

    async def _rev_parse(self, repo: Path, ref: str) -> str:
        result = await self._git(["-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"])
        return result.stdout.decode().strip()

    async def _checkout_state(self, bare: Path, path: Path, sha: str) -> CheckoutState:
        result = await self._git(["-C", str(bare), "log", "-1", "--format=%cI", sha])
        return CheckoutState(
            path=path,
            commit_sha=sha,
            committed_at=datetime.fromisoformat(result.stdout.decode().strip()),
        )

    async def prune_related(self, keep: set[str]) -> None:
        if not self._related_root.exists():
            return
        for owner_dir in self._related_root.iterdir():
            if not owner_dir.is_dir():
                continue
            for checkout in owner_dir.iterdir():
                if not checkout.is_dir():
                    continue
                full_name = f"{owner_dir.name}/{checkout.name}"
                if full_name in keep:
                    continue
                logger.info("related_pruned repo=%s", full_name)
                shutil.rmtree(checkout)
                self.related_stamp_path(full_name).unlink(missing_ok=True)
                bare = self._bare_for(full_name)
                if bare.exists():
                    await self._git(["-C", str(bare), "worktree", "prune"])

    async def read_symlink_target(self, pr: PullRequestRef, entry: TreeEntry) -> str:
        if not entry.is_symlink:
            raise GitError(f"{entry.path} is not a symlink (mode {entry.mode})")
        result = await self._git_bare(pr, ["cat-file", "blob", f"{pr.head_sha}:{entry.path}"])
        return result.stdout.decode("utf-8", errors="surrogateescape")

    async def diff_name_status(self, pr: PullRequestRef) -> list[NameStatusEntry]:
        result = await self._git_bare(
            pr,
            [
                "diff",
                "--name-status",
                "-z",
                "-M",
                "--no-color",
                f"{pr.base_sha}...{pr.head_sha}",
            ],
        )
        return _parse_name_status(result.stdout)

    async def default_branch_diff(self, pr: PullRequestRef) -> str:
        result = await self._git_bare(
            pr,
            [
                "diff",
                "--no-color",
                f"refs/rw/base/{pr.default_branch}...{pr.head_sha}",
            ],
        )
        return result.stdout.decode("utf-8", errors="replace")

    async def prune(self) -> None:
        if self._worktree_root.exists():
            for child in self._worktree_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
        if not self._bare_root.exists():
            return
        for bare in self._bare_root.rglob("*.git"):
            if bare.is_dir():
                await self._unlock_owned_worktrees(bare)
                await self._git(["-C", str(bare), "worktree", "prune"])

    async def _unlock_owned_worktrees(self, bare: Path) -> None:
        # A killed `worktree add` leaves an "initializing" lock, and prune skips locked entries.
        result = await self._git(["-C", str(bare), "worktree", "list", "--porcelain", "-z"])
        for record in result.stdout.split(b"\0\0"):
            fields = [
                field.decode("utf-8", errors="surrogateescape") for field in record.split(b"\0")
            ]
            path = next(
                (f.removeprefix("worktree ") for f in fields if f.startswith("worktree ")), None
            )
            locked = any(f == "locked" or f.startswith("locked ") for f in fields)
            if path is not None and locked and Path(path).is_relative_to(self._worktree_root_real):
                await self._git(["-C", str(bare), "worktree", "unlock", path])


def _parse_tree(raw: bytes) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, _, raw_path = record.partition(b"\t")
        mode = header.split(b" ", 1)[0].decode()
        entries.append(
            TreeEntry(path=raw_path.decode("utf-8", errors="surrogateescape"), mode=mode)
        )
    return entries


def _parse_name_status(raw: bytes) -> list[NameStatusEntry]:
    fields = [
        field.decode("utf-8", errors="surrogateescape") for field in raw.split(b"\0") if field
    ]
    entries: list[NameStatusEntry] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        code = status[:1]
        if code in ("R", "C"):
            old_path, new_path = fields[index + 1], fields[index + 2]
            entries.append(NameStatusEntry(status=code, path=new_path, old_path=old_path))
            index += 3
        else:
            entries.append(NameStatusEntry(status=code, path=fields[index + 1], old_path=None))
            index += 2
    return entries
