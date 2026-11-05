import asyncio
import shutil
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

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


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str


@dataclass(frozen=True)
class NameStatusEntry:
    status: str
    path: str
    old_path: str | None


class RepoCache:
    def __init__(self, *, git_binary: Path, bare_root: Path, worktree_root: Path) -> None:
        self._git_bin = str(git_binary)
        self._bare_root = bare_root
        self._worktree_root = worktree_root
        self._locks: dict[str, asyncio.Lock] = {}
        self._env = base_env(_GIT_ENV_EXTRA)

    def bare_path(self, pr: PullRequestRef) -> Path:
        return self._bare_root / pr.owner / f"{pr.repo}.git"

    def _lock_for(self, pr: PullRequestRef) -> asyncio.Lock:
        return self._locks.setdefault(pr.full_name, asyncio.Lock())

    async def _git(self, args: list[str], *, cwd: Path | None = None) -> CommandResult:
        return await run_command(
            [self._git_bin, *_GIT_HARDENING, *args],
            env=self._env,
            timeout_s=GIT_TIMEOUT_S,
            cwd=cwd,
        )

    async def _git_bare(self, pr: PullRequestRef, args: list[str]) -> CommandResult:
        return await self._git(["-C", str(self.bare_path(pr)), *args])

    async def ensure_fetched(self, pr: PullRequestRef) -> None:
        async with self._lock_for(pr):
            bare = self.bare_path(pr)
            if not bare.exists():
                bare.parent.mkdir(parents=True, exist_ok=True)
                await self._git(
                    [
                        "clone",
                        "--bare",
                        "--filter=blob:none",
                        "--",
                        f"git@github.com:{pr.full_name}.git",
                        str(bare),
                    ]
                )
            await self._git_bare(
                pr,
                [
                    "fetch",
                    "--filter=blob:none",
                    "origin",
                    f"+refs/pull/{pr.number}/head:refs/rw/pr/{pr.number}",
                    f"+refs/heads/{pr.base_ref}:refs/rw/base/{pr.base_ref}",
                ],
            )
            for sha in (pr.head_sha, pr.base_sha):
                await self._git_bare(pr, ["cat-file", "-e", f"{sha}^{{commit}}"])

    @asynccontextmanager
    async def worktree(self, pr: PullRequestRef) -> AsyncGenerator[Path]:
        await self.ensure_fetched(pr)
        path = self._worktree_root / f"{pr.owner}-{pr.repo}-{pr.number}-{pr.head_sha[:12]}"
        async with self._lock_for(pr):
            await self._git_bare(pr, ["worktree", "add", "--detach", str(path), pr.head_sha])
        try:
            yield path
        finally:
            await self._git_bare(pr, ["worktree", "remove", "--force", str(path)])

    async def list_tree(self, pr: PullRequestRef) -> list[TreeEntry]:
        result = await self._git_bare(pr, ["ls-tree", "-r", "-z", "--full-tree", pr.head_sha])
        entries: list[TreeEntry] = []
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            header, _, raw_path = record.partition(b"\t")
            mode = header.split(b" ", 1)[0].decode()
            entries.append(
                TreeEntry(path=raw_path.decode("utf-8", errors="surrogateescape"), mode=mode)
            )
        return entries

    async def read_blob(self, pr: PullRequestRef, entry: TreeEntry) -> bytes:
        if entry.mode in (_SYMLINK_MODE, _SUBMODULE_MODE):
            raise GitError(f"refusing to read non-blob entry {entry.path} (mode {entry.mode})")
        result = await self._git_bare(pr, ["cat-file", "blob", f"{pr.head_sha}:{entry.path}"])
        return result.stdout

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

    async def prune(self) -> None:
        if self._worktree_root.exists():
            for child in self._worktree_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
        if not self._bare_root.exists():
            return
        for bare in self._bare_root.rglob("*.git"):
            if bare.is_dir():
                await self._git(["-C", str(bare), "worktree", "prune"])


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
