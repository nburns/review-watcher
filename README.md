# review-watcher

A macOS tool that runs every few minutes under launchd. It finds open GitHub PRs where your review is requested, in an allowlist of repos. It reviews each one with the `claude` CLI, posts the result as a **pending** GitHub review (never submitted), and sends a macOS notification with a click-through link.

It also watches your own open PRs in the same repos and notifies you when someone else leaves a review or comment. Clicking the notification opens that review or comment on GitHub (a changes-requested review wins, then an approval, then other reviews, then the oldest new comment). New feedback on a PR is batched into one notification once that PR has had no new feedback for `quiet_minutes` (default 5), or after `max_wait_minutes` (default 30) at most. Feedback that existed before the first run is not notified.

A stacked PR (one whose base branch is the head of another open PR) is reviewed with the full default-branch-to-head diff as context, plus the open PRs below it in the stack. Findings and inline comments stay on the PR's own changes; interactions with lower PRs are anchored in this PR and reference the other PR by number. A PR into a long-lived branch, such as a release branch, is reviewed on its own diff: either no open PR has that branch as its head, or the branch is listed in the repo's `base_branches`.

A repo can be grouped with related repos (`[[related]]`). Each review of a group member gets read-only checkouts of the other members' default branches, a short map of each (its `repo_info` description, its root `AGENTS.md`, `CLAUDE.md`, or `README.md` capped at `related_guide_max_bytes`, and its skill index), and the diffs of up to five PRs in those repos that the PR body references as `owner/repo#N` or a PR URL. The reviewer decides which related files to read; findings stay on the PR's own diff and cite the other repo, and the review body lists the related files it used. Checkouts live in `var/related/<owner>/<repo>`, are shared by all reviews, and are refreshed at most once per cycle when older than `related_max_age_hours`. A related repo that cannot be fetched does not block the review; the body marks it unavailable.

Every review also applies a built-in operational lens (`src/review_watcher/lenses/sre.md`): failure handling, blast radius, capacity, detectability, health checks, deploy and rollback safety, worker lifecycle, and telemetry cost. It is routed through the repo's own system map and operational docs when they exist, so it works in repos without such skills. Turn it off per repo with `lenses = []`.

High and medium confidence findings must quote one verbatim line from the file they rely on. The quote is checked against git objects (PR head or base, or the related checkout's commit). A finding whose quote is missing or not found is moved to "Questions for the author" instead of being posted as a finding.

Repo guidance is loaded in priority order until `max_total_bytes` is spent: guidance files changed by the PR and instruction files and root guidance first, then review skills (the `SKILL.md` of `.claude/skills/*review*`). Other skills reach the prompt only as an index of path, name, and description from their `SKILL.md` frontmatter; the reviewer reads one with Read when it applies. Each review logs prompt bytes per section (`review_prompt` in the log).

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
| `approve <pr-url>` | Approve reviewing a gated author's PR at its current head; the next cycle reviews it |
| `status` | List review-requested PRs and their recorded state |
| `install-agent` / `uninstall-agent` | Manage the LaunchAgent |
| `prune` | Delete state older than 90 days, orphaned worktrees, and checkouts of repos no longer in a `[[related]]` group |
| `auth-login <name>` | Run an `[[auth]]` entry's `login` in this terminal, then re-check |

Global options: `--config PATH`, `--dry-run` (no posting, prints the review JSON for `review`), `--no-llm` (skip claude), `--verbose`.

## Development

Tasks live in `[tool.poe.tasks]` in `pyproject.toml`; `uv run poe` lists them.

| Task | Purpose |
| --- | --- |
| `setup` | `brew bundle`, then create `config.toml` from the example if missing |
| `install-agent` / `uninstall-agent` | Manage the LaunchAgent |
| `kick` | Run one launchd cycle now |
| `check` | Lint, format check, type check, and tests |
| `lint`, `format-check`, `types`, `test` | Each check on its own |

CI (`.github/workflows/ci.yml`) runs `poe check` on macOS for pushes to `main` and for pull requests.

## Security model

- The `claude` CLI runs in a PR worktree with `--restricted --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence`. Its tools are read-only (`Read,Grep,Glob`). It loads no settings, hooks, MCP servers, or slash commands from the repo. `--bare` is never used because it breaks keychain auth.
- Review guidance (`AGENTS.md`, `CLAUDE.md`, `.claude/skills`, and similar files) comes from the PR head's git objects. It is injected as delimited data with a random per-run delimiter. The review cannot remove built-in checks, lower severities, or approve.
- A PR that changes review or agent configuration (`guidance_watch` globs) gets a visible banner, a minimum risk floor (`medium` for review guidance, `high` for settings, hooks, and MCP config), and a `CONFIG CHANGED` notification title.
- Git runs with `core.hooksPath=/dev/null`, `protocol.file.allow=never`, `submodule.recurse=false`, and `GIT_LFS_SKIP_SMUDGE=1`. Submodules are never fetched.
- Related repos are read-only directories added with `--add-dir`; the tool set stays `Read,Grep,Glob`. Only repos in the same `[[related]]` group are checked out. PR references in the body to any other repo are logged and ignored, so a PR author cannot make the watcher clone arbitrary repos. Related guides and linked PR diffs are injected as delimited data, read from git objects.
- Reviews are posted as PENDING. You submit them yourself on GitHub.
- Operational context (logs, database schema) is fetched by plain Python in `context.py` before the review and injected as delimited, untrusted data. The LLM gets no new tools. Database access is schema and approximate row counts only, through fixed queries in a read-only session; table names come from the database itself. Credentials go through environment variables (`MYSQL_PWD`), never argv, the prompt, or logs. Log text is grouped and redacted (emails, bearer tokens, JWTs, AWS keys, long hex and base64) on a best-effort basis, and the rubric forbids quoting log lines or data values.

## Approval for listed authors

PRs by a login in `approval_required_authors` are not reviewed automatically. Once the debounce passes, the PR is marked `awaiting_approval` and you get one "Approve review?" notification per head SHA. Clicking it opens `var/approvals/approve-<owner>-<repo>-<number>.command`, which runs `review-watcher approve <url>` and records approval for the PR's current head. The next cycle reviews it. A new push needs a new approval. Ignored PRs are never reviewed. `review <url>` skips the gate.

## Per-author daily limit

`daily_review_limits` caps automatic reviews per GitHub login in any rolling 24 hours. A review counts when it starts, once per head SHA, so retries of the same head are free. Over the limit, the PR is marked `throttled` and you get one "Daily limit hit" notification for that head. A later cycle reviews it once the window has room. `review <url>` is not limited.

## Operational context and auth

A repo entry can list `context` sources (`gcloud_logs`, `aws_logs`, `mysql_schema`, `hoop_schema`), each needing one or more `[[auth]]` checks. Each cycle runs the needed checks once. If any fails, the PR is marked `awaiting_auth` (not a failure, no backoff), you get one notification per expired provider with its `hint`, and the PR is reviewed on the first later cycle where the check passes. A source that fails after auth passes does not block the review; the body lists it as unavailable. Run `install-agent` after changing auth or context so the plist `PATH` includes their tools.

An `[[auth]]` entry with a `login` command gets a clickable notification. The click opens `var/auth/<name>.command` in whichever app opens `.command` files, which is Terminal.app unless you change it. That script runs `review-watcher auth-login <name>`, which skips login if the check already passes, otherwise runs `login` and re-checks. The next cycle picks up the result. To use another terminal, open Get Info on any `.command` file, choose the app under "Open with", and click "Change All…". If you dismiss the notification, run `uv run review-watcher auth-login <name>` yourself. `config.toml` is trusted input: `check` and `login` run as you.

## Configuration

See `config.example.toml`. Relative paths resolve against the directory that holds `config.toml`, not the current directory. Unknown keys are rejected.

## Known limits

- The claude review step is subject to `timeout_s` and `max_diff_bytes`. A truncated diff is marked "not fully assessed" in the review body.
- Notifications use terminal-notifier. The `osascript` backend is a degraded fallback with no click-through.
- Only github.com over SSH is supported.
