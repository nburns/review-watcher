import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, PIPE

_BASE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "SSH_AUTH_SOCK",
)


class CommandFailed(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv: tuple[str, ...] = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:500]}")


class CommandTimeout(RuntimeError):
    def __init__(self, argv: Sequence[str], timeout_s: float) -> None:
        self.argv: tuple[str, ...] = tuple(argv)
        self.timeout_s = timeout_s
        super().__init__(f"{argv[0]} timed out after {timeout_s}s")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


def base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
    if extra is not None:
        env.update(extra)
    return env


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Process already exited between timeout and kill.
        return


async def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    cwd: Path | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=PIPE if stdin is not None else DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        cwd=cwd,
        env=dict(env),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
    except TimeoutError:
        _kill_group(process)
        await process.wait()
        raise CommandTimeout(argv, timeout_s) from None
    except asyncio.CancelledError:
        _kill_group(process)
        await process.wait()
        raise

    returncode = process.returncode
    if returncode is None:
        raise RuntimeError(f"{argv[0]} finished without a return code")
    result = CommandResult(argv=tuple(argv), returncode=returncode, stdout=stdout, stderr=stderr)
    if check and returncode != 0:
        raise CommandFailed(argv, returncode, stderr.decode("utf-8", errors="replace"))
    return result


async def run_interactive(argv: Sequence[str], *, env: Mapping[str, str], timeout_s: float) -> int:
    """Run with the caller's terminal attached and return the exit code."""
    # Same session as the caller so Ctrl-C in the terminal reaches the child.
    process = await asyncio.create_subprocess_exec(*argv, env=dict(env))
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        _kill(process)
        await process.wait()
        raise CommandTimeout(argv, timeout_s) from None
    except asyncio.CancelledError:
        _kill(process)
        await process.wait()
        raise
    return returncode


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        # Process already exited between timeout and kill.
        return
