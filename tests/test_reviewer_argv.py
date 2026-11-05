from pathlib import Path

import pytest

from review_watcher.config import ReviewerConfig
from review_watcher.reviewer import LockdownViolation, assert_lockdown, build_claude_argv


def _argv() -> list[str]:
    return build_claude_argv(
        binary=Path("/opt/homebrew/bin/claude"),
        config=ReviewerConfig(),
        schema_json="{}",
        system_prompt="rubric",
    )


def test_lockdown_flags_are_present_and_bare_is_absent() -> None:
    argv = _argv()

    assert_lockdown(argv)
    assert "--bare" not in argv


def test_missing_lockdown_flag_is_rejected() -> None:
    argv = [arg for arg in _argv() if arg != "--restricted"]

    with pytest.raises(LockdownViolation, match="--restricted"):
        assert_lockdown(argv)


def test_bare_flag_is_rejected() -> None:
    with pytest.raises(LockdownViolation, match="--bare"):
        assert_lockdown([*_argv(), "--bare"])
