from pathlib import Path

from review_watcher.notifier import Notification, TerminalNotifierNotifier


async def test_text_fields_are_escaped_for_terminal_notifier(tmp_path: Path) -> None:
    record = tmp_path / "argv"
    stub = tmp_path / "terminal-notifier"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n')
    stub.chmod(0o755)

    await TerminalNotifierNotifier(stub).notify(
        Notification(
            title="(dry run) LOW risk: o/r#1",
            message="[ACT-5154] sanitize html",
            group="o/r#1",
            open_url="https://github.com/o/r/pull/1/files",
            subtitle="-2 comments",
        )
    )

    argv = record.read_text().splitlines()
    sent = dict(zip(argv[::2], argv[1::2], strict=True))
    assert sent == {
        "-title": "\\(dry run) LOW risk: o/r#1",
        "-message": "\\[ACT-5154] sanitize html",
        "-group": "o/r#1",
        "-subtitle": "\\-2 comments",
        "-open": "https://github.com/o/r/pull/1/files",
    }
