import asyncio
import logging
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from review_watcher.config import AuthConfig
from review_watcher.launcher import CommandLauncher
from review_watcher.notifier import Notification, Notifier, notify_logging_failures
from review_watcher.proc import CommandTimeout, base_env, run_command, run_interactive
from review_watcher.state import StateStore

AUTH_CHECK_TIMEOUT_S = 30.0
LOGIN_TIMEOUT_S = 15 * 60.0
AUTH_TITLE_PREFIX = "Auth needed: "

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthResult:
    name: str
    ok: bool
    detail: str


class AuthGate(Protocol):
    async def missing(self, required: Sequence[AuthConfig]) -> list[AuthConfig]: ...


def trace_to_stderr(argv: Sequence[str]) -> None:
    print(f"+ {shlex.join(argv)}", file=sys.stderr, flush=True)


async def check_auth(
    auth: AuthConfig, *, trace: Callable[[Sequence[str]], None] | None = None
) -> AuthResult:
    if trace is not None:
        trace(auth.check)
    try:
        result = await run_command(
            auth.check,
            env={**base_env(), **auth.env},
            timeout_s=AUTH_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, CommandTimeout) as exc:
        outcome = AuthResult(name=auth.name, ok=False, detail=str(exc))
    else:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        first = stderr.splitlines()[0][:200] if stderr else ""
        outcome = AuthResult(
            name=auth.name,
            ok=result.returncode == 0,
            detail=f"exit {result.returncode}: {first}",
        )
    logger.info("auth_check name=%s ok=%s detail=%s", auth.name, outcome.ok, outcome.detail)
    return outcome


@dataclass(frozen=True)
class SignInOutcome:
    # None when the check already passed and login was skipped.
    login_exit: int | None
    result: AuthResult


async def sign_in(
    auth: AuthConfig, *, trace: Callable[[Sequence[str]], None] | None = None
) -> SignInOutcome:
    if auth.login is None:
        raise ValueError(f"auth {auth.name!r} has no login command configured")
    before = await check_auth(auth, trace=trace)
    if before.ok:
        return SignInOutcome(login_exit=None, result=before)
    if trace is not None:
        trace(auth.login)
    login_exit = await run_interactive(
        auth.login, env={**base_env(), **auth.env}, timeout_s=LOGIN_TIMEOUT_S
    )
    return SignInOutcome(login_exit=login_exit, result=await check_auth(auth, trace=trace))


class AuthChecker:
    """Runs each check once per instance, so one instance per run cycle polls each provider once."""

    def __init__(
        self, *, state: StateStore, notifier: Notifier, launcher: CommandLauncher | None
    ) -> None:
        self._state = state
        self._notifier = notifier
        self._launcher = launcher
        self._results: dict[str, asyncio.Task[AuthResult]] = {}

    async def missing(self, required: Sequence[AuthConfig]) -> list[AuthConfig]:
        for auth in required:
            if auth.name not in self._results:
                self._results[auth.name] = asyncio.create_task(self._check(auth))
        results = [await self._results[auth.name] for auth in required]
        return [auth for auth, result in zip(required, results, strict=True) if not result.ok]

    async def _check(self, auth: AuthConfig) -> AuthResult:
        outcome = await check_auth(auth)
        if self._state.record_auth_check(auth.name, ok=outcome.ok):
            await notify_logging_failures(self._notifier, self._notification(auth))
        return outcome

    def _notification(self, auth: AuthConfig) -> Notification:
        waiting = "Reviews that need it wait until it works."
        script = self._login_script(auth)
        return Notification(
            title=f"{AUTH_TITLE_PREFIX}{auth.name}",
            message=f"{auth.hint}. {waiting}" if script is None else f"Click to sign in. {waiting}",
            group=f"review-watcher-auth-{auth.name}",
            open_url=None if script is None else script.as_uri(),
        )

    def _login_script(self, auth: AuthConfig) -> Path | None:
        if auth.login is None or self._launcher is None:
            return None
        try:
            return self._launcher.write(auth.name, ["auth-login", auth.name])
        except OSError:
            logger.exception("login_launcher_failed name=%s", auth.name)
            return None
