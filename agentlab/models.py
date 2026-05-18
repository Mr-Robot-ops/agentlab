from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskType(str, Enum):
    DOCS = "docs"
    TESTS = "tests"
    BUGFIX = "bugfix"
    FEATURE = "feature"
    DEPENDENCY = "dependency"
    AUTH = "auth"
    DATABASE_MIGRATION = "database_migration"
    CI = "ci"
    INFRA = "infra"
    SECURITY = "security"
    REFACTOR = "refactor"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReportStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Verdict(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"


class FindingSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentTask(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1)
    task_type: TaskType = TaskType.UNKNOWN
    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_score: int = Field(default=0, ge=0)
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    test_requirements: list[str] = Field(default_factory=list)
    approved: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_safe_id(cls, value: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if any(char not in allowed for char in value):
            raise ValueError("task id may only contain letters, numbers, hyphen and underscore")
        return value

    @field_validator("affected_files", "forbidden_actions", "test_requirements")
    @classmethod
    def validate_relative_values(cls, values: list[str]) -> list[str]:
        for value in values:
            if Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError(f"unsafe relative value: {value}")
        return values


class TaskPlan(StrictModel):
    summary: str = ""
    tasks: list[AgentTask] = Field(default_factory=list)
    source_signals: list[str] = Field(default_factory=list)


class PatchProposal(StrictModel):
    task_id: str
    summary: str
    patch: str = Field(min_length=1)
    affected_files: list[str] = Field(default_factory=list)
    expected_tests: list[str] = Field(default_factory=list)
    risk_score: int = Field(default=0, ge=0)
    rollback: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiffStats(StrictModel):
    changed_files: list[str] = Field(default_factory=list)
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)
    touched_protected_paths: list[str] = Field(default_factory=list)
    secrets_touched: bool = False


class RiskAssessment(StrictModel):
    score: int = Field(ge=0)
    level: RiskLevel
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)
    touched_secret_paths: list[str] = Field(default_factory=list)


class CommandResult(StrictModel):
    command: str
    cwd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0, ge=0)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class TestReport(StrictModel):
    status: ReportStatus
    passed: bool
    commands: list[CommandResult] = Field(default_factory=list)
    logs_excerpt: str = ""
    coverage_note: str = ""
    recommendation: str = ""


class Finding(StrictModel):
    tool: str
    severity: FindingSeverity
    title: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    description: str = ""
    blocked: bool = False


class BuildSecurityReport(StrictModel):
    status: ReportStatus
    passed: bool
    docker_build: CommandResult | None = None
    compose_config: CommandResult | None = None
    scanners: list[CommandResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    recommendation: str = ""


class ReviewComment(StrictModel):
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    body: str
    severity: FindingSeverity = FindingSeverity.MEDIUM


class ReviewReport(StrictModel):
    reviewer: Literal["quality", "security_architecture"]
    verdict: Verdict
    summary: str
    comments: list[ReviewComment] = Field(default_factory=list)
    risk_score_delta: int = 0


class ImplementationReport(StrictModel):
    task_id: str
    branch: str
    status: ReportStatus
    applied: bool = False
    pushed: bool = False
    commit_sha: str | None = None
    patch_summary: str = ""
    changed_files: list[str] = Field(default_factory=list)
    risk_score: int = Field(default=0, ge=0)
    tests_recommended: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class MergeRequestInfo(StrictModel):
    mr_id: int
    iid: int | None = None
    title: str
    web_url: str | None = None
    source_branch: str
    target_branch: str
    labels: list[str] = Field(default_factory=list)


class GateDecision(StrictModel):
    allowed: bool
    mode: Literal["merge_request", "direct_main_push"]
    verdict: Literal["allowed", "blocked"]
    risk_score: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    policy_checks: dict[str, bool] = Field(default_factory=dict)


class RollbackReport(StrictModel):
    status: ReportStatus
    commit_sha: str | None = None
    pipeline_status: str | None = None
    revert_branch: str | None = None
    revert_commit_sha: str | None = None
    incident_summary: str = ""
    recommended_action: str = ""


class AuditEvent(StrictModel):
    run_id: str
    agent: str
    action: str
    status: Literal["started", "succeeded", "failed", "skipped", "blocked"]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float | None = Field(default=None, ge=0)
    input_hash: str | None = None
    output_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AgentRunStatus(StrictModel):
    agent: str
    state: Literal["pending", "running", "passed", "failed", "skipped", "blocked"] = "pending"
    current_action: str | None = None
    last_action: str | None = None
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    last_error: str | None = None
    event_count: int = 0


class RunStatusSnapshot(StrictModel):
    run_id: str
    state: Literal["pending", "running", "passed", "failed", "blocked"] = "pending"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    current_agent: str | None = None
    current_action: str | None = None
    agents: dict[str, AgentRunStatus] = Field(default_factory=dict)
    last_event: AuditEvent | None = None
    audit_file: str
    events_file: str


class ArtifactRecord(StrictModel):
    name: str
    path: str
    sha256: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactManifest(StrictModel):
    run_id: str
    artifacts: list[ArtifactRecord] = Field(default_factory=list)


class PreflightCheck(StrictModel):
    name: str
    status: Literal["passed", "warning", "failed", "skipped"]
    message: str
    remediation: str | None = None


class PreflightReport(StrictModel):
    mode: str
    passed: bool
    checks: list[PreflightCheck] = Field(default_factory=list)


class RepoPolicy(StrictModel):
    version: int = 1
    protected_paths: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    forbidden_task_types: list[str] = Field(default_factory=list)
    required_test_commands: list[str] = Field(default_factory=list)
    max_changed_files: int | None = Field(default=None, ge=1)
    max_added_lines: int | None = Field(default=None, ge=1)
    max_deleted_lines: int | None = Field(default=None, ge=1)
    max_risk_score_for_merge: int | None = Field(default=None, ge=0)
    max_risk_score_for_direct_main_push: int | None = Field(default=None, ge=0)
    block_auto_merge: bool = False
    block_direct_main_push: bool = True

    @field_validator("protected_paths", "allowed_task_types", "forbidden_task_types", "required_test_commands")
    @classmethod
    def normalize_policy_strings(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if stripped and stripped not in normalized:
                normalized.append(stripped)
        return normalized
