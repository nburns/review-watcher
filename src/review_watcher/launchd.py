import os
import plistlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from review_watcher.config import LoadedConfig
from review_watcher.proc import base_env, run_command

LABEL = "com.nburns.review-watcher"
SYSTEM_PATH_DIRS: tuple[str, ...] = ("/usr/bin", "/bin")
LAUNCHCTL_TIMEOUT_S = 30.0
_SERVICE_NOT_FOUND = 113


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_path_env(binaries: Sequence[Path]) -> str:
    dirs: list[str] = []
    for binary in binaries:
        parent = str(binary.parent)
        if parent not in dirs:
            dirs.append(parent)
    for system_dir in SYSTEM_PATH_DIRS:
        if system_dir not in dirs:
            dirs.append(system_dir)
    return ":".join(dirs)


def build_plist(
    *,
    program: Path,
    loaded: LoadedConfig,
    interval_minutes: int,
    path_env: str,
    home: Path,
) -> dict[str, Any]:
    log_dir = loaded.var_dir / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(program),
            "--config",
            str(loaded.config_path),
            "run",
        ],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "WorkingDirectory": str(loaded.base_dir),
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": path_env, "HOME": str(home)},
        # Background priority throttles disk I/O; a large checkout went from 6s to 137s+.
        "ProcessType": "Standard",
    }


def write_plist(path: Path, plist: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)


def read_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data: dict[str, Any] = plistlib.load(handle)
    return data


async def is_loaded(domain: str) -> bool:
    result = await run_command(
        ["launchctl", "print", f"{domain}/{LABEL}"],
        env=base_env(),
        timeout_s=LAUNCHCTL_TIMEOUT_S,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == _SERVICE_NOT_FOUND:
        return False
    stderr = result.stderr.decode("utf-8", errors="replace")
    raise RuntimeError(f"launchctl print failed ({result.returncode}): {stderr.strip()}")


async def bootout(domain: str, plist: Path) -> None:
    # bootout of an unloaded job fails with a generic EIO (5) on recent macOS, so check first.
    if not await is_loaded(domain):
        return
    await run_command(
        ["launchctl", "bootout", domain, str(plist)],
        env=base_env(),
        timeout_s=LAUNCHCTL_TIMEOUT_S,
    )


async def bootstrap(domain: str, plist: Path) -> None:
    await run_command(
        ["launchctl", "bootstrap", domain, str(plist)],
        env=base_env(),
        timeout_s=LAUNCHCTL_TIMEOUT_S,
    )


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def venv_program(loaded: LoadedConfig) -> Path:
    program = loaded.base_dir / ".venv" / "bin" / "review-watcher"
    if not program.is_file():
        raise FileNotFoundError(f"{program} not found; run `uv sync` in {loaded.base_dir}")
    return program
