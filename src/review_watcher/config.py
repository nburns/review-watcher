import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def _as_list(value: object) -> object:
    return [value] if isinstance(value, str) else value


class AuthConfig(_StrictModel):
    # The name becomes a launcher filename, so it must not carry path separators.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    check: list[str] = Field(min_length=1)
    hint: str = Field(min_length=1)
    login: Annotated[list[str], Field(min_length=1)] | None = None
    env: dict[str, str] = Field(default_factory=dict)


class _SourceBase(_StrictModel):
    auth: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=40_000, gt=0)

    @field_validator("auth", mode="before")
    @classmethod
    def _auth_as_list(cls, value: object) -> object:
        return _as_list(value)


class GcloudLogsSource(_SourceBase):
    kind: Literal["gcloud_logs"]
    project: str = Field(min_length=1)
    filter: str = Field(min_length=1)
    freshness: str = "24h"
    limit: int = Field(default=200, gt=0)


class AwsLogsSource(_SourceBase):
    kind: Literal["aws_logs"]
    profile: str = Field(min_length=1)
    region: str = Field(min_length=1)
    log_groups: list[str] = Field(min_length=1)
    filter_pattern: str = ""
    window_hours: int = Field(default=24, gt=0)
    limit: int = Field(default=200, gt=0)


class SecretRef(_StrictModel):
    project: str = Field(min_length=1)
    name: str = Field(min_length=1)


class MysqlSchemaSource(_SourceBase):
    kind: Literal["mysql_schema"]
    host: str = Field(min_length=1)
    port: int = Field(default=3306, gt=0)
    secret: SecretRef
    max_tables: int = Field(default=15, gt=0)


class HoopSchemaSource(_SourceBase):
    kind: Literal["hoop_schema"]
    connections: list[str] = Field(min_length=1)
    max_tables: int = Field(default=15, gt=0)


ContextSource = Annotated[
    GcloudLogsSource | AwsLogsSource | MysqlSchemaSource | HoopSchemaSource,
    Field(discriminator="kind"),
]


class RepoConfig(_StrictModel):
    name: str
    instructions: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, name: str) -> str:
        if not REPO_SLUG_RE.fullmatch(name):
            raise ValueError(f"repo must be 'owner/name': {name!r}")
        return name.lower()

    @field_validator("instructions")
    @classmethod
    def _validate_instructions(cls, instructions: list[str]) -> list[str]:
        cleaned = [sentence.strip() for sentence in instructions]
        if any(sentence == "" for sentence in cleaned):
            raise ValueError("instructions contains an empty sentence")
        return cleaned


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
    repos: list[RepoConfig] = Field(min_length=1)
    include_team_requests: bool = True
    skip_drafts: bool = True
    interval_minutes: int = Field(default=10, ge=2)
    debounce_minutes: int = Field(default=5, ge=0)
    concurrency: int = Field(default=2, ge=1)
    existing_pending_policy: Literal["skip", "replace"] = "skip"
    rereview_on_push: bool = True
    max_attempts: int = Field(default=3, ge=1)
    sensitive_paths: list[str] = Field(default_factory=list)
    approval_required_authors: list[str] = Field(default_factory=list)
    daily_review_limits: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)
    guidance: GuidanceConfig = Field(default_factory=GuidanceConfig)
    guidance_watch: GuidanceWatchConfig = Field(default_factory=GuidanceWatchConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    auth: list[AuthConfig] = Field(default_factory=list)
    context: dict[str, ContextSource] = Field(default_factory=dict)

    @field_validator("repos", mode="before")
    @classmethod
    def _expand_plain_names(cls, repos: object) -> object:
        if not isinstance(repos, list):
            return repos
        entries = cast("list[object]", repos)
        return [{"name": repo} if isinstance(repo, str) else repo for repo in entries]

    @field_validator("repos")
    @classmethod
    def _reject_duplicate_repos(cls, repos: list[RepoConfig]) -> list[RepoConfig]:
        names = [repo.name for repo in repos]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"repos lists the same repo more than once: {', '.join(duplicates)}")
        return repos

    @field_validator("approval_required_authors")
    @classmethod
    def _normalize_authors(cls, authors: list[str]) -> list[str]:
        cleaned = [author.strip().lower() for author in authors]
        if any(author == "" for author in cleaned):
            raise ValueError("approval_required_authors contains an empty login")
        return cleaned

    @field_validator("daily_review_limits")
    @classmethod
    def _normalize_limit_logins(cls, limits: dict[str, int]) -> dict[str, int]:
        normalized = {login.strip().lower(): limit for login, limit in limits.items()}
        if "" in normalized:
            raise ValueError("daily_review_limits contains an empty login")
        if len(normalized) != len(limits):
            raise ValueError("daily_review_limits lists the same login more than once")
        return normalized

    @model_validator(mode="after")
    def _check_references(self) -> "Config":
        auth_names = [auth.name for auth in self.auth]
        duplicates = sorted({name for name in auth_names if auth_names.count(name) > 1})
        if duplicates:
            raise ValueError(f"auth lists the same name more than once: {', '.join(duplicates)}")
        for source_name, source in self.context.items():
            unknown = [name for name in source.auth if name not in auth_names]
            if unknown:
                raise ValueError(f"context {source_name!r} uses unknown auth: {', '.join(unknown)}")
        for repo in self.repos:
            missing = [name for name in repo.context if name not in self.context]
            if missing:
                raise ValueError(f"repo {repo.name} uses unknown context: {', '.join(missing)}")
        return self

    @property
    def repo_names(self) -> list[str]:
        return [repo.name for repo in self.repos]

    def _repo(self, full_name: str) -> RepoConfig:
        for repo in self.repos:
            if repo.name == full_name.lower():
                return repo
        raise KeyError(f"{full_name} is not in the configured repos")

    def instructions_for(self, full_name: str) -> list[str]:
        return list(self._repo(full_name).instructions)

    def context_for(self, full_name: str) -> dict[str, ContextSource]:
        return {name: self.context[name] for name in self._repo(full_name).context}

    def auth_for(self, sources: dict[str, ContextSource]) -> list[AuthConfig]:
        needed = {name for source in sources.values() for name in source.auth}
        return [auth for auth in self.auth if auth.name in needed]

    def requires_approval(self, author: str) -> bool:
        # GitHub logins are case-insensitive.
        return author.lower() in self.approval_required_authors

    def daily_limit_for(self, author: str) -> int | None:
        return self.daily_review_limits.get(author.lower())

    def allows_repo(self, full_name: str) -> bool:
        # GitHub returns repo names in their canonical case; the allowlist is stored lowercased.
        return full_name.lower() in self.repo_names


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
