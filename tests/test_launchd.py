import os
import plistlib
import subprocess
from pathlib import Path

from review_watcher.config import LoadedConfig
from review_watcher.launchd import (
    LABEL,
    build_guard_script,
    build_path_env,
    build_plist,
    guard_script_path,
    write_guard_script,
    write_plist,
)
from tests.fakes import make_config


def _loaded(tmp_path: Path) -> LoadedConfig:
    return LoadedConfig(
        settings=make_config(interval_minutes=10), config_path=tmp_path / "config.toml"
    )


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/zsh", str(script)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_plist_round_trips_with_expected_keys(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    script = guard_script_path(loaded)
    plist = build_plist(
        script=script,
        loaded=loaded,
        interval_minutes=10,
        path_env=build_path_env([Path("/opt/homebrew/bin/gh")]),
        home=tmp_path,
    )
    target = tmp_path / f"{LABEL}.plist"
    write_plist(target, plist)

    with target.open("rb") as handle:
        loaded_plist = plistlib.load(handle)

    assert loaded_plist == plist
    assert loaded_plist["Label"] == LABEL
    assert loaded_plist["StartInterval"] == 600
    assert loaded_plist["RunAtLoad"] is True
    assert loaded_plist["ProgramArguments"] == ["/bin/zsh", str(script)]
    assert loaded_plist["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert loaded_plist["ProcessType"] == "Standard"
    assert "LowPriorityIO" not in loaded_plist


def test_guard_notifies_once_while_venv_missing_then_runs_program(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    program = tmp_path / "with space" / ".venv" / "bin" / "review-watcher"
    ran = tmp_path / "ran"
    notified = tmp_path / "notified"
    notify = tmp_path / "notify"
    _stub(notify, f'printf "%s\\n" "$@" >> "{notified}"')
    script = guard_script_path(loaded)
    write_guard_script(
        script,
        build_guard_script(
            program=program, loaded=loaded, notify_argv=[str(notify), "title", "it's down"]
        ),
    )

    first = _run(script)
    second = _run(script)

    assert first.returncode != 0
    assert second.returncode != 0
    assert "uv sync" in first.stderr
    assert notified.read_text().splitlines() == ["title", "it's down"]

    _stub(program.parent / "python", "exit 0")
    _stub(program, f'printf "%s\\n" "$@" > "{ran}"')

    recovered = _run(script)

    assert recovered.returncode == 0
    assert ran.read_text().splitlines() == ["--config", str(loaded.config_path), "run"]

    (program.parent / "python").unlink()
    broken = _run(script)

    assert broken.returncode != 0
    assert notified.read_text().splitlines() == ["title", "it's down"] * 2
