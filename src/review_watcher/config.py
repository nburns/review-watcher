import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

DEFAULT_REVIEW_GUIDANCE_GLOBS: tuple[str, ...] = (
    ".claude/skills/**",
    ".claude/commands/**",
    ".claude/agents/**",
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "CONTRIBUTING*",
    "REVIEWING*",
    "docs/**/*review*",
    ".github/PULL_REQUEST_TEMPLATE*",
    ".github/copilot-instructions.md",
    ".cursor/rules/**",
    "CODEOWNERS",
    ".github/CODEOWNERS",
)

DEFAULT_AGENT_EXECUTION_CONFIG_GLOBS: tuple[str, ...] = (
    ".claude/settings*.json",
    ".claude/hooks/**",
    ".mcp.json",
    "**/.mcp.json",
    ".claude/**/*.sh",
    ".gitattributes",
    ".gitmodules",
    ".lfsconfig",
    ".githooks/**",
    ".envrc",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewerConfig(_StrictModel):
    backend: Literal["claude_cli"] = "claude_cli"
    model: str = "opus"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    timeout_s: int = Field(default=600, gt=0)
    max_inline_comments: int = Field(default=10, ge=0)
    max_diff_bytes: int = Field(default=200_000, gt=0)


class GuidanceConfig(_StrictModel):
    extra_paths: list[str] = Field(default_factory=list)
    max_file_bytes: int = Field(default=60_000, gt=0)
    max_total_bytes: int = Field(default=200_000, gt=0)


class GuidanceWatchConfig(_StrictModel):
    review_guidance: list[str] = Field(default_factory=lambda: list(DEFAULT_REVIEW_GUIDANCE_GLOBS))
    agent_execution_config: list[str] = Field(
        default_factory=lambda: list(DEFAULT_AGENT_EXECUTION_CONFIG_GLOBS)
    )


class NotifierConfig(_StrictModel):
    backend: Literal["terminal_notifier", "osascript"] = "terminal_notifier"


class FeedbackConfig(_StrictModel):
    enabled: bool = True
    ignore_authors: list[str] = Field(default_factory=list)
    quiet_minutes: int = Field(default=5, ge=0)
    max_wait_minutes: int = Field(default=30, ge=0)


class PathsConfig(_StrictModel):
    var_dir: Path = Path("var")


class Config(_StrictModel):
    github_login: str = Field(min_length=1)
    repos: list[str] = Field(min_length=1)
    include_team_requests: bool = True
    skip_drafts: bool = True
    interval_minutes: int = Field(default=10, ge=2)
    debounce_minutes: int = Field(default=5, ge=0)
    concurrency: int = Field(default=2, ge=1)
    existing_pending_policy: Literal["skip", "replace"] = "skip"
    rereview_on_push: bool = True
    max_attempts: int = Field(default=3, ge=1)
    sensitive_paths: list[str] = Field(default_factory=list)
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)
    guidance: GuidanceConfig = Field(default_factory=GuidanceConfig)
    guidance_watch: GuidanceWatchConfig = Field(default_factory=GuidanceWatchConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @field_validator("repos")
    @classmethod
    def _validate_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            if not REPO_SLUG_RE.fullmatch(repo):
                raise ValueError(f"repo must be 'owner/name': {repo!r}")
        return [repo.lower() for repo in repos]

    def allows_repo(self, full_name: str) -> bool:
        # GitHub returns repo names in their canonical case; the allowlist is stored lowercased.
        return full_name.lower() in self.repos


@dataclass(frozen=True)
class LoadedConfig:
    settings: Config
    config_path: Path

    @property
    def base_dir(self) -> Path:
        return self.config_path.parent

    @property
    def var_dir(self) -> Path:
        configured = self.settings.paths.var_dir
        if configured.is_absolute():
            return configured
        return self.base_dir / configured


def load_config(path: Path) -> LoadedConfig:
    absolute = path.expanduser().resolve()
    with absolute.open("rb") as handle:
        data = tomllib.load(handle)
    return LoadedConfig(settings=Config.model_validate(data), config_path=absolute)
