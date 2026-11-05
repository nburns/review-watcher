# review-watcher

A macOS tool that runs every few minutes under launchd. It finds open GitHub PRs where your review is requested, in an allowlist of repos. It reviews each one with the `claude` CLI, posts the result as a **pending** GitHub review (never submitted), and sends a macOS notification with a click-through link.

It also watches your own open PRs in the same repos and notifies you when someone else leaves a review or comment. Clicking the notification opens that review or comment on GitHub (a changes-requested review wins, then an approval, then other reviews, then the oldest new comment). New feedback on a PR is batched into one notification once that PR has had no new feedback for `quiet_minutes` (default 5), or after `max_wait_minutes` (default 30) at most. Feedback that existed before the first run is not notified.

The LLM is used only for the review step. Discovery, diffing, guidance collection, risk policy, posting, and state are plain deterministic Python.

## Setup

```sh
uv run poe setup                 # brew bundle, then config.toml from the example if missing
$EDITOR config.toml              # set github_login and repos
uv run review-watcher --dry-run review https://github.com/owner/name/pull/1
uv run poe install-agent
```

Then grant notification permission to terminal-notifier in System Settings > Notifications. Use `uv run poe kick` to run a cycle immediately and check `var/logs/`. `uv run poe` lists all tasks.

`install-agent` checks that `.venv/bin/review-watcher`, `terminal-notifier`, and SSH access to the first allowlisted repo all work, then writes `~/Library/LaunchAgents/com.nburns.review-watcher.plist` and loads it.

## Commands

| Command | Purpose |
| --- | --- |
| `run` | One cycle: review requests plus feedback on your PRs (what launchd runs) |
| `review <pr-url> [--force]` | Review one PR now. Posts a pending review unless `--dry-run` |
| `status` | List review-requested PRs and their recorded state |
| `install-agent` / `uninstall-agent` | Manage the LaunchAgent |
| `prune` | Delete state older than 90 days and orphaned worktrees |

Global options: `--config PATH`, `--dry-run` (no posting, prints the review JSON for `review`), `--no-llm` (skip claude), `--verbose`.

## Security model

- The `claude` CLI runs in a PR worktree with `--restricted --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence`. Its tools are read-only (`Read,Grep,Glob`). It loads no settings, hooks, MCP servers, or slash commands from the repo. `--bare` is never used because it breaks keychain auth.
- Review guidance (`AGENTS.md`, `CLAUDE.md`, `.claude/skills`, and similar files) comes from the PR head's git objects. It is injected as delimited data with a random per-run delimiter. The review cannot remove built-in checks, lower severities, or approve.
- A PR that changes review or agent configuration (`guidance_watch` globs) gets a visible banner, a minimum risk floor (`medium` for review guidance, `high` for settings, hooks, and MCP config), and a `CONFIG CHANGED` notification title.
- Git runs with `core.hooksPath=/dev/null`, `protocol.file.allow=never`, `submodule.recurse=false`, and `GIT_LFS_SKIP_SMUDGE=1`. Submodules are never fetched.
- Reviews are posted as PENDING. You submit them yourself on GitHub.

## Configuration

See `config.example.toml`. Relative paths resolve against the directory that holds `config.toml`, not the current directory. Unknown keys are rejected.

## Known limits

- The claude review step is subject to `timeout_s` and `max_diff_bytes`. A truncated diff is marked "not fully assessed" in the review body.
- Notifications use terminal-notifier. The `osascript` backend is a degraded fallback with no click-through.
- Only github.com over SSH is supported.
