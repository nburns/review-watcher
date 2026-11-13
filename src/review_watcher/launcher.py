import os
import shlex
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from review_watcher.proc import base_env


@dataclass(frozen=True)
class CommandLauncher:
    """Writes .command scripts that run review-watcher; macOS opens them in the .command app."""

    directory: Path
    program: Path
    config_path: Path

    def write(self, stem: str, args: Sequence[str]) -> Path:
        if len(args) == 0:
            raise ValueError("launcher needs a review-watcher subcommand")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        target = self.directory / f"{stem}.command"
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=f".{stem}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._script(args))
            tmp.chmod(0o700)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def _script(self, args: Sequence[str]) -> str:
        lines = ["#!/bin/zsh", "set -euo pipefail"]
        path = base_env().get("PATH")
        if path is not None:
            lines.append(f"export PATH={shlex.quote(path)}")
        command = shlex.join([str(self.program), "--config", str(self.config_path), *args])
        failed = shlex.quote(f"{args[0]} failed with exit")
        lines.append(f"print -r -- {shlex.quote(f'+ {command}')} >&2")
        lines.append(f"{command} && exit 0")
        lines.append("rc=$?")
        lines.append(f'print -r -- {failed} "$rc" >&2')
        lines.append('print -rn -- "Press Enter to close: "')
        lines.append("read -r || true")
        lines.append('exit "$rc"')
        return "\n".join(lines) + "\n"
