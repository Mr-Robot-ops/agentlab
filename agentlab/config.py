from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def derive_project_path_from_repo_url(repo_url: str) -> str | None:
    value = repo_url.strip()
    if not value:
        return None
    if "://" not in value and ":" in value and "@" in value.split(":", 1)[0]:
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        path = parsed.path
    path = unquote(path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path or None


def normalize_project_id(project_id: int | str) -> int | str:
    if isinstance(project_id, int):
        return project_id
    value = unquote(str(project_id).strip()).strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return int(value) if value.isdigit() else value


def gitlab_project_api_id(project_id: int | str) -> int | str:
    normalized = normalize_project_id(project_id)
    if isinstance(normalized, int):
        return normalized
    from urllib.parse import quote

    return quote(normalized, safe="")


class OllamaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:11434"
    models: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "qwen3.6:35b",
            "planner": "qwen3.6:35b",
            "implementer": "qwen3.6:35b",
            "mr_agent": "qwen3.6:35b",
            "review_quality": "qwen3.6:35b",
            "review_security": "qwen3.6:35b",
        }
    )

    def model_for(self, agent_name: str) -> str:
        return self.models.get(agent_name, self.models.get("default", "qwen3.6:35b"))


class AutoApproveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_risk_score: int = Field(default=3, ge=0)
    allowed_task_types: list[str] = Field(default_factory=lambda: ["docs", "tests"])
    allowed_paths: list[str] = Field(
        default_factory=lambda: [
            "README.md",
            "docs/**",
            "tests/**",
            "rust-backend/tests/**",
            "web/src/**/*.test.ts",
        ]
    )
    blocked_paths: list[str] = Field(
        default_factory=lambda: [
            ".gitlab-ci.yml",
            "deploy/**",
            "Dockerfile",
            "compose.yaml",
            "**/.env",
        ]
    )
    max_changed_files: int = Field(default=5, ge=1)
    require_tests_for_code: bool = True

    @field_validator("allowed_task_types", "allowed_paths", "blocked_paths")
    @classmethod
    def normalize_strings(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]


class ScheduleEntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cron: str


class ScheduleLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_open_agent_mrs: int = Field(default=2, ge=0)
    max_new_mrs_per_day: int = Field(default=1, ge=0)
    min_hours_between_action_runs: int = Field(default=8, ge=0)


class ScheduleBehaviorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip_if_open_agent_mr_exists: bool = True
    skip_if_default_branch_unchanged_since_last_plan: bool = True


class ScheduleReviewCommentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cron: str = "*/10 * * * *"
    process_history: bool = False
    max_comments_per_run: int = Field(default=1, ge=1)
    cooldown_minutes: int = Field(default=10, ge=0)
    allowed_commands: list[str] = Field(
        default_factory=lambda: ["revise", "fix", "status", "explain", "stop", "resume"]
    )
    allowed_authors: list[str] = Field(default_factory=list)
    require_author_role: list[str] = Field(default_factory=lambda: ["owner", "maintainer"])

    @field_validator("allowed_commands", "allowed_authors", "require_author_role")
    @classmethod
    def normalize_strings(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip().lower()
            if stripped and stripped not in normalized:
                normalized.append(stripped)
        return normalized


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timezone: str = "Europe/Berlin"
    watch: ScheduleEntryConfig = Field(default_factory=lambda: ScheduleEntryConfig(cron="*/30 * * * *"))
    plan: ScheduleEntryConfig = Field(default_factory=lambda: ScheduleEntryConfig(cron="0 7,19 * * *"))
    action: ScheduleEntryConfig = Field(default_factory=lambda: ScheduleEntryConfig(cron="30 2 * * *"))
    limits: ScheduleLimitsConfig = Field(default_factory=ScheduleLimitsConfig)
    behavior: ScheduleBehaviorConfig = Field(default_factory=ScheduleBehaviorConfig)
    review_comments: ScheduleReviewCommentsConfig = Field(default_factory=ScheduleReviewCommentsConfig)

    @model_validator(mode="before")
    @classmethod
    def fill_partial_entries(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        defaults = {
            "watch": {"enabled": True, "cron": "*/30 * * * *"},
            "plan": {"enabled": True, "cron": "0 7,19 * * *"},
            "action": {"enabled": True, "cron": "30 2 * * *"},
            "review_comments": {
                "enabled": False,
                "cron": "*/10 * * * *",
                "process_history": False,
                "max_comments_per_run": 1,
                "cooldown_minutes": 10,
                "allowed_commands": ["revise", "fix", "status", "explain", "stop", "resume"],
                "allowed_authors": [],
                "require_author_role": ["owner", "maintainer"],
            },
        }
        merged = dict(raw)
        for key, default in defaults.items():
            if isinstance(merged.get(key), dict):
                merged[key] = {**default, **merged[key]}
        return merged


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    gitlab_url: str
    project_id: int | str | None = None
    default_branch: str = "main"
    gitlab_token_env: str = "GITLAB_TOKEN"
    target_repo_path: Path
    target_repo_url: str | None = None
    target_repo_ref: str | None = None
    clone_target_repo: bool = False
    clone_depth: int = Field(default=0, ge=0)
    reject_embedded_git_credentials: bool = True
    workspace_root: Path = Path("./runs")
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    auto_approve: AutoApproveConfig = Field(default_factory=AutoApproveConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    allowed_commands: list[str] = Field(default_factory=list)
    forbidden_commands: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    forbidden_task_types: list[str] = Field(default_factory=list)
    required_test_commands: list[str] = Field(default_factory=list)
    repo_policy_file: str = ".agentlab.yaml"
    require_repo_policy_for_write: bool = False
    repo_index_ignore: list[str] = Field(
        default_factory=lambda: [
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            ".pytest_cache",
            "__pycache__",
            ".mypy_cache",
            ".ruff_cache",
            "coverage",
        ]
    )
    max_index_files: int = Field(default=5000, ge=1)
    max_index_file_bytes: int = Field(default=250_000, ge=1)
    max_index_todos: int = Field(default=200, ge=0)
    supply_chain_enabled: bool = True
    provenance_enabled: bool = True
    require_lockfiles_for_merge: bool = False
    max_changed_files: int = Field(default=20, ge=1)
    max_added_lines: int = Field(default=500, ge=1)
    max_deleted_lines: int = Field(default=500, ge=1)
    max_risk_score_for_merge: int = Field(default=60, ge=0)
    max_risk_score_for_direct_main_push: int = Field(default=10, ge=0)
    auto_merge_enabled: bool = False
    direct_main_push_enabled: bool = False
    require_two_reviewers: bool = True
    require_two_testers: bool = True
    push_agent_branches_enabled: bool = False
    docker_build_enabled: bool = True
    docker_compose_enabled: bool = True
    command_timeout_seconds: int = Field(default=900, ge=1)
    audit_file: str = "audit.jsonl"

    @model_validator(mode="before")
    @classmethod
    def derive_project_id_from_repo_url(cls, raw: Any) -> Any:
        if isinstance(raw, dict) and not raw.get("project_id"):
            repo_url = raw.get("target_repo_url")
            if repo_url:
                derived = derive_project_path_from_repo_url(str(repo_url))
                if derived:
                    raw = {**raw, "project_id": derived}
        return raw

    @model_validator(mode="after")
    def validate_project_id_present(self) -> "AppConfig":
        if self.project_id is None:
            raise ValueError("project_id is required unless it can be derived from target_repo_url")
        self.project_id = normalize_project_id(self.project_id)
        return self

    @field_validator(
        "protected_paths",
        "allowed_commands",
        "forbidden_commands",
        "allowed_task_types",
        "forbidden_task_types",
        "required_test_commands",
        "repo_index_ignore",
    )
    @classmethod
    def normalize_strings(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    def agent_model(self, agent_name: str) -> str:
        return self.ollama.model_for(agent_name)


DEFAULT_ALLOWED_COMMANDS = [
    "python -m pytest",
    "pytest",
    "npm test",
    "pnpm test",
    "go test ./...",
    "cargo test",
    "docker build",
    "docker compose config",
    "docker compose up",
    "docker compose logs",
    "docker compose down",
    "trivy",
    "gitleaks",
    "semgrep",
    "bandit",
    "npm audit",
]

DEFAULT_FORBIDDEN_COMMANDS = [
    "rm -rf",
    "git reset --hard",
    "git push --force",
    "docker run --privileged",
    "docker compose up --privileged",
]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw.setdefault("allowed_commands", DEFAULT_ALLOWED_COMMANDS)
    raw.setdefault("forbidden_commands", DEFAULT_FORBIDDEN_COMMANDS)

    base_dir = config_path.parent
    for key in ("target_repo_path", "workspace_root"):
        if key in raw:
            value = Path(raw[key]).expanduser()
            if not value.is_absolute():
                value = (base_dir / value).resolve()
            raw[key] = value

    return AppConfig.model_validate(raw)
