import stat
from pathlib import Path

import pytest

from review_watcher.github import GhCliGitHub
from review_watcher.proc import CommandFailed
from tests.fakes import make_pr


def _flaky_gh(tmp_path: Path, *, failures: int, stderr: str) -> Path:
    counter = tmp_path / "calls"
    script = tmp_path / "gh"
    script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f'echo x >> "{counter}"\n'
        f'if [ "$(wc -l < "{counter}")" -le {failures} ]; then\n'
        f"  echo '{stderr}' >&2\n"
        "  exit 1\n"
        "fi\n"
        "echo '[[]]'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _calls(tmp_path: Path) -> int:
    return len((tmp_path / "calls").read_text().splitlines())


async def _no_sleep(_: float) -> None:
    return None


async def test_read_retries_transient_network_error(tmp_path: Path) -> None:
    gh = _flaky_gh(
        tmp_path,
        failures=2,
        stderr='Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
    )
    client = GhCliGitHub(gh, sleep=_no_sleep)

    reviews = await client.list_reviews(make_pr(7, "h" * 40, "b" * 40))

    assert reviews == []
    assert _calls(tmp_path) == 3


async def test_read_gives_up_after_retries(tmp_path: Path) -> None:
    gh = _flaky_gh(tmp_path, failures=10, stderr="HTTP 502: Bad Gateway")
    client = GhCliGitHub(gh, sleep=_no_sleep)

    with pytest.raises(CommandFailed):
        await client.list_reviews(make_pr(7, "h" * 40, "b" * 40))
    assert _calls(tmp_path) == 3


async def test_non_transient_error_is_not_retried(tmp_path: Path) -> None:
    gh = _flaky_gh(tmp_path, failures=1, stderr="HTTP 404: Not Found")
    client = GhCliGitHub(gh, sleep=_no_sleep)

    with pytest.raises(CommandFailed):
        await client.list_reviews(make_pr(7, "h" * 40, "b" * 40))
    assert _calls(tmp_path) == 1


async def test_write_is_never_retried(tmp_path: Path) -> None:
    gh = _flaky_gh(tmp_path, failures=1, stderr="net/http: TLS handshake timeout")
    client = GhCliGitHub(gh, sleep=_no_sleep)

    with pytest.raises(CommandFailed):
        await client.delete_review(make_pr(7, "h" * 40, "b" * 40), 123)
    assert _calls(tmp_path) == 1
