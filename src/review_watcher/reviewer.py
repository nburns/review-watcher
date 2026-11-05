import json
import secrets
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError

from review_watcher import rubric
from review_watcher.config import ReviewerConfig
from review_watcher.models import ReviewContext, ReviewDraft, RiskLevel
from review_watcher.proc import base_env, run_command

CLAUDE_MIN_VERSION: tuple[int, int, int] = (2, 1, 311)
CLAUDE_TIMEOUT_S = 30.0
READ_ONLY_TOOLS = "Read,Grep,Glob"
REQUIRED_CLAUDE_FLAGS: tuple[str, ...] = (
    "--json-schema",
    "--output-format",
    "--tools",
    "--allowedTools",
    "--restricted",
    "--strict-mcp-config",
    "--setting-sources",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--append-system-prompt",
)
FORBIDDEN_CLAUDE_FLAGS: tuple[str, ...] = ("--bare",)
_RAW_OUTPUT_LOG_LIMIT = 2000


class ReviewOutputInvalid(RuntimeError):
    pass


class LockdownViolation(RuntimeError):
    pass


class Reviewer(Protocol):
    async def review(self, ctx: ReviewContext) -> ReviewDraft: ...


class NullReviewer:
    async def review(self, ctx: ReviewContext) -> ReviewDraft:
        assessment = None
        if ctx.guidance_changes.changes:
            assessment = "Not assessed: review ran with --no-llm."
        return ReviewDraft(
            summary="No LLM review was run (--no-llm).",
            proposed_risk=RiskLevel.LOW,
            risk_rationale="No review was performed.",
            findings=[],
            guidance_sources_used=[],
            guidance_change_assessment=assessment,
            questions_for_author=[],
        )


def resolve_claude_binary() -> Path:
    found = shutil.which("claude", path=base_env().get("PATH"))
    if found is None:
        raise FileNotFoundError("claude CLI not found on PATH")
    return Path(found)


def parse_claude_version(version_output: str) -> tuple[int, int, int]:
    first = version_output.strip().split(" ", 1)[0]
    parts = first.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"cannot parse claude version from {version_output!r}")
    major, minor, patch = (int(part) for part in parts)
    return (major, minor, patch)


async def verify_claude_version(binary: Path) -> tuple[int, int, int]:
    result = await run_command(
        [str(binary), "--version"], env=base_env(), timeout_s=CLAUDE_TIMEOUT_S
    )
    version = parse_claude_version(result.stdout.decode("utf-8", errors="replace"))
    if version < CLAUDE_MIN_VERSION:
        raise RuntimeError(
            f"claude {version} is older than the required minimum {CLAUDE_MIN_VERSION}"
        )
    return version


def build_claude_argv(
    *,
    binary: Path,
    config: ReviewerConfig,
    schema_json: str,
    system_prompt: str,
) -> list[str]:
    return [
        str(binary),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--model",
        config.model,
        "--effort",
        config.effort,
        "--tools",
        READ_ONLY_TOOLS,
        "--allowedTools",
        READ_ONLY_TOOLS,
        "--restricted",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--append-system-prompt",
        system_prompt,
    ]


def assert_lockdown(argv: Sequence[str]) -> None:
    for flag in REQUIRED_CLAUDE_FLAGS:
        if flag not in argv:
            raise LockdownViolation(f"claude argv is missing required flag {flag}")
    for flag in FORBIDDEN_CLAUDE_FLAGS:
        if flag in argv:
            raise LockdownViolation(f"claude argv contains forbidden flag {flag}")


def parse_review_output(stdout: bytes, ctx: ReviewContext) -> ReviewDraft:
    raw_text = stdout.decode("utf-8", errors="replace")
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReviewOutputInvalid(f"claude output is not JSON: {_clip(raw_text)}") from exc
    if not isinstance(envelope, dict):
        raise ReviewOutputInvalid(f"claude output is not a JSON object: {_clip(raw_text)}")
    fields = cast("dict[str, object]", envelope)
    if fields.get("is_error") is not False:
        raise ReviewOutputInvalid(
            f"claude reported an error (subtype={fields.get('subtype')!r}): {_clip(raw_text)}"
        )
    if "structured_output" not in fields:
        raise ReviewOutputInvalid(f"claude output has no structured_output: {_clip(raw_text)}")
    structured: object = fields["structured_output"]
    if structured is None:
        raise ReviewOutputInvalid(f"claude structured_output is null: {_clip(raw_text)}")
    try:
        draft = ReviewDraft.model_validate(structured)
    except ValidationError as exc:
        raise ReviewOutputInvalid(f"structured_output failed validation: {exc}") from exc

    if ctx.guidance_changes.changes:
        assessment = draft.guidance_change_assessment
        if assessment is None or not assessment.strip():
            raise ReviewOutputInvalid(
                "guidance changes exist but guidance_change_assessment is empty"
            )
    return draft


def _clip(text: str) -> str:
    return text[:_RAW_OUTPUT_LOG_LIMIT]


class ClaudeCliReviewer:
    def __init__(self, *, binary: Path, config: ReviewerConfig) -> None:
        self._binary = binary
        self._config = config
        self._env = base_env()

    async def review(self, ctx: ReviewContext) -> ReviewDraft:
        nonce = secrets.token_hex(8)
        system_prompt = rubric.build_system_prompt(
            guidance=ctx.guidance,
            head_sha=ctx.pr.head_sha,
            nonce=nonce,
            max_inline_comments=ctx.max_inline_comments,
        )
        argv = build_claude_argv(
            binary=self._binary,
            config=self._config,
            schema_json=rubric.output_schema_json(),
            system_prompt=system_prompt,
        )
        assert_lockdown(argv)
        result = await run_command(
            argv,
            env=self._env,
            timeout_s=self._config.timeout_s,
            cwd=ctx.worktree_path,
            stdin=rubric.build_user_prompt(ctx, nonce).encode("utf-8"),
        )
        return parse_review_output(result.stdout, ctx)
