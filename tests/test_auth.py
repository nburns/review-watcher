import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from review_watcher.auth import AuthChecker, sign_in
from review_watcher.config import AuthConfig
from review_watcher.launcher import CommandLauncher
from review_watcher.state import StateStore
from tests.fakes import FakeNotifier


def _state(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db", clock=lambda: datetime(2026, 11, 9, tzinfo=UTC))


def _stub(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def test_launcher_script_runs_auth_login_with_awkward_paths(tmp_path: Path) -> None:
    root = tmp_path / "it's a $dir"
    root.mkdir()
    args_file = tmp_path / "args"
    program = _stub(root / "review-watcher", f'printf "%s\\n" "$@" > "{args_file}"')
    config_path = root / "config `x`.toml"
    launcher = CommandLauncher(directory=root / "auth", program=program, config_path=config_path)

    script = launcher.write("gcloud", ["auth-login", "gcloud"])
    run = subprocess.run(
        [str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
    )

    assert script == root / "auth" / "gcloud.command"
    assert script.stat().st_mode & 0o777 == 0o700
    assert (root / "auth").stat().st_mode & 0o777 == 0o700
    assert args_file.read_text().splitlines() == [
        "--config",
        str(config_path),
        "auth-login",
        "gcloud",
    ]
    traced = shlex.join([str(program), "--config", str(config_path), "auth-login", "gcloud"])
    assert run.stderr.splitlines()[0] == f"+ {traced}"


async def test_failing_auth_with_login_notifies_with_a_clickable_launcher(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    launcher = CommandLauncher(
        directory=tmp_path / "auth", program=tmp_path / "rw", config_path=tmp_path / "c.toml"
    )
    checker = AuthChecker(state=_state(tmp_path), notifier=notifier, launcher=launcher)
    with_login = AuthConfig(name="gcloud", check=["/usr/bin/false"], hint="h", login=["true"])
    without_login = AuthConfig(name="hoop", check=["/usr/bin/false"], hint="Run hoop login")

    missing = await checker.missing([with_login, without_login])

    assert [auth.name for auth in missing] == ["gcloud", "hoop"]
    urls = {n.title: n.open_url for n in notifier.sent}
    assert urls == {
        "Auth needed: gcloud": (tmp_path / "auth" / "gcloud.command").as_uri(),
        "Auth needed: hoop": None,
    }


async def test_sign_in_skips_login_when_already_signed_in(tmp_path: Path) -> None:
    marker = tmp_path / "login-ran"
    login = _stub(tmp_path / "login", f'touch "{marker}"')
    auth = AuthConfig(name="gcloud", check=["/usr/bin/true"], hint="h", login=[str(login)])

    outcome = await sign_in(auth)

    assert outcome.login_exit is None
    assert outcome.result.ok
    assert not marker.exists()


async def test_sign_in_runs_login_then_reports_the_recheck(tmp_path: Path) -> None:
    authed = tmp_path / "authed"
    login = _stub(tmp_path / "login", f'touch "{authed}"')
    auth = AuthConfig(
        name="gcloud", check=["/bin/test", "-e", str(authed)], hint="h", login=[str(login)]
    )

    traced: list[list[str]] = []

    outcome = await sign_in(auth, trace=lambda argv: traced.append(list(argv)))

    assert outcome.login_exit == 0
    assert outcome.result.ok
    assert traced == [auth.check, auth.login, auth.check]


def test_approval_launcher_runs_approve_with_the_pr_url(tmp_path: Path) -> None:
    args_file = tmp_path / "args"
    program = _stub(tmp_path / "review-watcher", f'printf "%s\\n" "$@" > "{args_file}"')
    launcher = CommandLauncher(
        directory=tmp_path / "approvals", program=program, config_path=tmp_path / "c.toml"
    )
    url = "https://github.com/acme/app/pull/1"

    script = launcher.write("approve-acme-app-1", ["approve", url])
    subprocess.run([str(script)], stdin=subprocess.DEVNULL, capture_output=True, check=False)

    assert args_file.read_text().splitlines() == [
        "--config",
        str(tmp_path / "c.toml"),
        "approve",
        url,
    ]


def test_launcher_exits_quietly_on_success_and_holds_the_error_on_failure(
    tmp_path: Path,
) -> None:
    ok = CommandLauncher(
        directory=tmp_path / "ok",
        program=_stub(tmp_path / "rw-ok", "exit 0"),
        config_path=tmp_path / "c.toml",
    )
    bad = CommandLauncher(
        directory=tmp_path / "bad",
        program=_stub(tmp_path / "rw-bad", "echo boom >&2; exit 3"),
        config_path=tmp_path / "c.toml",
    )

    passed = subprocess.run(
        [str(ok.write("gcloud", ["auth-login", "gcloud"]))],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    failed = subprocess.run(
        [str(bad.write("gcloud", ["auth-login", "gcloud"]))],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert passed.returncode == 0
    assert "Press Enter" not in passed.stdout
    assert failed.returncode == 3
    assert "boom" in failed.stderr
    assert "auth-login failed with exit 3" in failed.stderr
    assert "Press Enter to close" in failed.stdout
