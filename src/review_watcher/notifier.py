import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from review_watcher.proc import CommandFailed, CommandTimeout, base_env, run_command

NOTIFY_TIMEOUT_S = 20.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    group: str
    open_url: str | None
    subtitle: str | None = None


class Notifier(Protocol):
    async def notify(self, notification: Notification) -> None: ...


class TerminalNotifierNotifier:
    def __init__(self, binary: Path) -> None:
        self._binary = str(binary)

    async def notify(self, notification: Notification) -> None:
        argv = [
            self._binary,
            "-title",
            _escape(notification.title),
            "-message",
            _escape(notification.message),
            "-group",
            notification.group,
        ]
        if notification.subtitle is not None:
            argv.extend(["-subtitle", _escape(notification.subtitle)])
        if notification.open_url is not None:
            argv.extend(["-open", notification.open_url])
        await run_command(argv, env=base_env(), timeout_s=NOTIFY_TIMEOUT_S)


def _escape(text: str) -> str:
    # NSUserDefaults misparses values starting with [ ( { " -; terminal-notifier strips one "\".
    return "\\" + text


class OsascriptNotifier:
    """Degraded fallback: shows a notification without a click action and logs the URL."""

    def __init__(self, binary: Path) -> None:
        self._binary = str(binary)

    async def notify(self, notification: Notification) -> None:
        if notification.open_url is not None:
            logger.info("notification_url url=%s", notification.open_url)
        await run_command(
            [
                self._binary,
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                "--",
                notification.title,
                notification.message,
            ],
            env=base_env(),
            timeout_s=NOTIFY_TIMEOUT_S,
        )


async def notify_logging_failures(notifier: Notifier, notification: Notification) -> None:
    """A failed notification is logged, not raised, so it never aborts review or state work."""
    try:
        await notifier.notify(notification)
    except (OSError, CommandFailed, CommandTimeout, TimeoutError):
        logger.exception("notification_failed title=%s", notification.title)


def resolve_binary(name: str) -> Path:
    found = shutil.which(name, path=base_env().get("PATH"))
    if found is None:
        raise FileNotFoundError(f"{name} not found on PATH; install it (see Brewfile)")
    return Path(found)
