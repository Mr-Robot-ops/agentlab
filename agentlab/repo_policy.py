from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentlab.config import AppConfig
from agentlab.models import RepoPolicy


def load_repo_policy(repo_path: str | Path, policy_file: str) -> RepoPolicy | None:
    path = Path(repo_path) / policy_file
    if not path.exists():
        return None
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RepoPolicy.model_validate(raw)


def apply_repo_policy(config: AppConfig, policy: RepoPolicy | None) -> AppConfig:
    if policy is None:
        return config

    updates: dict[str, object] = {
        "protected_paths": _merge_unique(config.protected_paths, policy.protected_paths),
        "allowed_task_types": _merge_allowed(config.allowed_task_types, policy.allowed_task_types),
        "forbidden_task_types": _merge_unique(config.forbidden_task_types, policy.forbidden_task_types),
        "required_test_commands": _merge_unique(config.required_test_commands, policy.required_test_commands),
        "auto_merge_enabled": config.auto_merge_enabled and not policy.block_auto_merge,
        "direct_main_push_enabled": config.direct_main_push_enabled and not policy.block_direct_main_push,
    }

    for field_name in (
        "max_changed_files",
        "max_added_lines",
        "max_deleted_lines",
        "max_risk_score_for_merge",
        "max_risk_score_for_direct_main_push",
    ):
        configured = getattr(config, field_name)
        policy_value = getattr(policy, field_name)
        if policy_value is not None:
            updates[field_name] = min(configured, policy_value)

    return config.model_copy(update=updates)


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    merged = list(left)
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


def _merge_allowed(configured: list[str], policy: list[str]) -> list[str]:
    if configured and policy:
        return [item for item in configured if item in policy]
    return configured or policy
