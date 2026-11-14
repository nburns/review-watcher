from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.NIT: 3,
}


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
)


def max_risk(first: RiskLevel, second: RiskLevel) -> RiskLevel:
    return max(first, second, key=_RISK_ORDER.index)


class Category(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTS = "tests"
    ERROR_HANDLING = "error_handling"
    API_COMPAT = "api_compat"
    PERFORMANCE = "performance"
    CONCURRENCY = "concurrency"
    READABILITY = "readability"
    DOCS = "docs"
    RELIABILITY = "reliability"
    OBSERVABILITY = "observability"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
}


class Tier(StrEnum):
    REVIEW_GUIDANCE = "review_guidance"
    AGENT_EXECUTION_CONFIG = "agent_execution_config"


class PullRequestRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner: str
    repo: str
    number: int
    head_sha: str
    head_ref: str
    base_ref: str
    base_sha: str
    default_branch: str
    url: str
    title: str
    author: str
    is_draft: bool
    body: str | None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def is_stacked(self) -> bool:
        return self.base_ref != self.default_branch


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    text: str


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: Category
    path: str
    line: int = Field(ge=1)
    side: Side
    start_line: int | None
    title: str
    body: str
    evidence: str
    quote: Quote | None
    confidence: Confidence


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    proposed_risk: RiskLevel
    risk_rationale: str
    findings: list[Finding]
    guidance_sources_used: list[str]
    related_sources_used: list[str]
    guidance_change_assessment: str | None
    questions_for_author: list[str]


class GuidanceChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    old_path: str | None
    status: str
    tier: Tier


class GuidanceChanges(BaseModel):
    model_config = ConfigDict(frozen=True)

    changes: list[GuidanceChange]


class GuidanceFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    text: str
    truncated: bool
    modified_in_pr: bool
    pr_diff: str | None


class ReviewGuidance(BaseModel):
    model_config = ConfigDict(frozen=True)

    files: list[GuidanceFile]
    omitted_paths: list[str]


class InlineComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    line: int
    side: Side
    start_line: int | None
    body: str


class FinalReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    pr: PullRequestRef
    risk: RiskLevel
    model_risk: RiskLevel
    floor_reasons: list[str]
    body: str
    inline_comments: list[InlineComment]
    guidance_changes: list[GuidanceChange]

    @property
    def guidance_changed(self) -> bool:
        return bool(self.guidance_changes)

    @property
    def guidance_tier_max(self) -> Tier | None:
        if any(change.tier == Tier.AGENT_EXECUTION_CONFIG for change in self.guidance_changes):
            return Tier.AGENT_EXECUTION_CONFIG
        if self.guidance_changes:
            return Tier.REVIEW_GUIDANCE
        return None


class PostedReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_id: int
    html_url: str


class FeedbackKind(StrEnum):
    REVIEW = "review"
    INLINE_COMMENT = "inline_comment"
    COMMENT = "comment"


class FeedbackItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: FeedbackKind
    item_id: int
    author: str
    review_state: str | None
    url: str
    created_at: datetime

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.item_id}"


class ReviewSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_id: int
    state: str
    author: str
    body: str


class StackContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    default_branch: str
    ancestors: list[PullRequestRef]
    diff_text: str
    diff_truncated: bool


class ContextResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: str
    text: str
    truncated: bool
    error: str | None


class RelatedGuide(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    text: str
    truncated: bool


class RelatedCheckout(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    path: Path
    branch: str
    commit_sha: str
    committed_at: datetime
    guide: RelatedGuide | None


class RelatedRepo(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str
    description: str
    checkout: RelatedCheckout | None
    error: str | None


class LinkedPullRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str
    number: int
    title: str
    state: str
    url: str
    diff_text: str
    diff_truncated: bool


class LinkFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    error: str


class RelatedContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: list[RelatedRepo]
    linked_prs: list[LinkedPullRequest]
    link_failures: list[LinkFailure]

    @property
    def readable_paths(self) -> list[Path]:
        return [repo.checkout.path for repo in self.repos if repo.checkout is not None]


class ReviewContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pr: PullRequestRef
    diff_text: str
    diff_truncated: bool
    changed_files: list[str]
    worktree_path: Path
    guidance: ReviewGuidance
    guidance_changes: GuidanceChanges
    max_inline_comments: int
    stack: StackContext | None
    repo_instructions: list[str]
    operational_context: list[ContextResult]
    related: RelatedContext | None
    lenses: list[str]
