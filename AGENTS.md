# AGENTS.md

Guidance for agents working in this repo.

## Commands

- Install and sync: `uv sync`
- Lint: `uv run ruff check`
- Format check: `uv run ruff format --check`
- Types: `uv run basedpyright`
- Tests: `uv run pytest`
- CLI help: `uv run review-watcher --help`

All four checks must pass clean before a change is done. `uv run poe check` runs them in order.

## Conventions

- Python 3.13, src layout under `src/review_watcher/`, tests under `tests/`.
- Raise on errors. Do not return sentinel values (`None`, `""`, `[]`, `0`) that could also be valid results.
- Missing, null and empty are distinct. Check them explicitly.
- Structured results use named fields (dataclasses or pydantic), never positional tuples.
- Build JSON and plist payloads from native data and serialize them.
- Every subprocess call goes through `proc.run_command` with an explicit env and timeout.
- Comments: none by default. Only the non-obvious why, one line, ASCII hyphens.

## Invariants

- The LLM is used only in `reviewer.py`. An AST test enforces that no other module references the `claude` binary.
- The `claude` argv must keep every lockdown flag and must never contain `--bare`. `assert_lockdown` enforces this at call time.
- Git calls on repo content go through `RepoCache._git`, which applies the hardening `-c` flags and `GIT_LFS_SKIP_SMUDGE=1`.
- Only `FinalReview` is posted to GitHub, and the posted review has no `event` field, so it stays PENDING.
- Repo files are read from the PR head's git objects, not the working tree.
