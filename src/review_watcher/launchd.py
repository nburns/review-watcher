import os
import plistlib
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from review_watcher.config import LoadedConfig
from review_watcher.proc import base_env, run_command

LABEL = "com.nburns.review-watcher"
SYSTEM_PATH_DIRS: tuple[str, ...] = ("/usr/bin", "/bin")
LAUNCHCTL_TIMEOUT_S = 30.0
_SERVICE_NOT_FOUND = 113
VENV_RENOTIFY_MINUTES = 60


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


def guard_script_path(loaded: LoadedConfig) -> Path:
    return loaded.var_dir / "launchd" / "run-agent.zsh"


def build_guard_script(*, program: Path, loaded: LoadedConfig, notify_argv: Sequence[str]) -> str:
    """launchd fails silently with EX_CONFIG when the venv is gone, so check it and notify."""
    marker = guard_script_path(loaded).parent / "venv-missing.notified"
    command = shlex.join([str(program), "--config", str(loaded.config_path), "run"])
    python = program.parent / "python"
    problem = f"{program} or its interpreter is missing; run `uv sync` in {loaded.base_dir}"
    lines = [
        "#!/bin/zsh",
        "set -euo pipefail",
        f"marker={shlex.quote(str(marker))}",
        f"if [[ -x {shlex.quote(str(program))} && -x {shlex.quote(str(python))} ]]; then",
        '  rm -f -- "$marker"',
        f"  exec {command}",
        "fi",
        f"print -r -- {shlex.quote(f'review-watcher agent: {problem}')} >&2",
        f'if [[ ! -e "$marker" || -n "$(find "$marker" -mmin +{VENV_RENOTIFY_MINUTES})" ]]; then',
        f"  {shlex.join(notify_argv)} || print -r -- 'review-watcher agent: notify failed' >&2",
        '  touch -- "$marker"',
        "fi",
        "exit 1",
    ]
    return "\n".join(lines) + "\n"


def write_guard_script(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def build_plist(
    *,
    script: Path,
    loaded: LoadedConfig,
    interval_minutes: int,
    path_env: str,
    home: Path,
) -> dict[str, Any]:
    log_dir = loaded.var_dir / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", str(script)],
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
