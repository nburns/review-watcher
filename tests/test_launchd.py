import plistlib
from pathlib import Path

from review_watcher.config import LoadedConfig
from review_watcher.launchd import LABEL, build_path_env, build_plist, write_plist
from tests.fakes import make_config


def test_plist_round_trips_with_expected_keys(tmp_path: Path) -> None:
    loaded = LoadedConfig(
        settings=make_config(interval_minutes=10), config_path=tmp_path / "config.toml"
    )
    plist = build_plist(
        program=tmp_path / ".venv" / "bin" / "review-watcher",
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
    assert loaded_plist["ProgramArguments"][-1] == "run"
    assert loaded_plist["ProgramArguments"][1:3] == ["--config", str(loaded.config_path)]
    assert loaded_plist["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert loaded_plist["ProcessType"] == "Standard"
    assert "LowPriorityIO" not in loaded_plist
