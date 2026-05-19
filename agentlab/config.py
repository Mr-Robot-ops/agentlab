from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    gitlab_url: str
    project_id: int | str
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
