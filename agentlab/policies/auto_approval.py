from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from agentlab.config import AppConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.policies.risk import detect_secret_paths


POLICY_NAME = "auto_approval"
POLICY_VERSION = "1"
DEFAULT_FORBIDDEN_ACTIONS = [
    "Do not change files outside affected_files.",
    "Do not touch secrets, credentials, deployment, CI, Docker, Kubernetes, or protected paths.",
    "Do not enable direct-main pushes or auto-merge.",
]
RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
TYPE_ORDER = {TaskType.DOCS: 0, TaskType.TESTS: 1, TaskType.REFACTOR: 2}


class AutoApprovalPolicy:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.policy = config.auto_approve

    def apply(self, plan: TaskPlan) -> tuple[TaskPlan, dict[str, Any]]:
        if not self.policy.enabled:
            report = {
                "enabled": False,
                "policy_name": POLICY_NAME,
                "policy_version": POLICY_VERSION,
                "evaluated_tasks": [
                    {
                        "task_id": task.id,
                        "approved": task.approved,
                        "reasons": ["auto_approve_disabled"],
                        "details": self._details(task),
                    }
                    for task in plan.tasks
                ],
                "approved_tasks": [task.id for task in plan.tasks if task.approved],
                "rejected_tasks": [
                    {
                        "task_id": task.id,
                        "reasons": ["auto_approve_disabled"],
                        "details": self._details(task),
                    }
                    for task in plan.tasks
                    if not task.approved
                ],
                "selected_task_id": None,
                "policy_config": self.policy.model_dump(mode="json"),
            }
            return plan, report

        evaluated = []
        approved_ids: list[str] = []
        rejected: list[dict[str, Any]] = []
        updated_tasks = []

        for task in plan.tasks:
            approved, reasons, details, updated_task = self._evaluate(task)
            updated_tasks.append(updated_task)
            evaluated.append({"task_id": task.id, "approved": approved, "reasons": reasons, "details": details})
            if approved:
                approved_ids.append(task.id)
            else:
                rejected.append({"task_id": task.id, "reasons": reasons, "details": details})

        approved_tasks = [task for task in updated_tasks if task.approved]
        selected = self.select_task(approved_tasks)
        report = {
            "enabled": self.policy.enabled,
            "policy_name": POLICY_NAME,
            "policy_version": POLICY_VERSION,
            "evaluated_tasks": evaluated,
            "approved_tasks": approved_ids,
            "rejected_tasks": rejected,
            "selected_task_id": selected.id if selected else None,
            "policy_config": self.policy.model_dump(mode="json"),
        }
        return plan.model_copy(update={"tasks": updated_tasks}), report

    @staticmethod
    def select_task(tasks: list[AgentTask]) -> AgentTask | None:
        if not tasks:
            return None
        return sorted(
            tasks,
            key=lambda task: (
                task.risk_score,
                RISK_ORDER.get(task.risk_level, 99),
                TYPE_ORDER.get(task.task_type, 99),
                task.id,
            ),
        )[0]

    def _evaluate(self, task: AgentTask) -> tuple[bool, list[str], dict[str, Any], AgentTask]:
        reasons: list[str] = []
        details = self._details(task)
        if task.approved:
            return True, ["already_approved"], details, self._with_metadata(task, False, ["already_approved"])

        if self.config.direct_main_push_enabled or self.config.auto_merge_enabled:
            reasons.append("unsafe_flow_flags_enabled")
        if task.risk_score > self.policy.max_risk_score:
            reasons.append("risk_score_too_high")
        if task.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            reasons.append("risk_level_too_high")
        if task.task_type.value not in self.policy.allowed_task_types:
            reasons.append("task_type_not_allowed")
        if not task.affected_files:
            reasons.append("missing_affected_files")
        if len(task.affected_files) > self.policy.max_changed_files:
            reasons.append("too_many_affected_files")
        if detect_secret_paths(task.affected_files):
            reasons.append("secret_sensitive_path")
        blocked = [path for path in task.affected_files if self._matches_any(path, self.policy.blocked_paths)]
        if blocked:
            reasons.append("blocked_path")
        outside = [path for path in task.affected_files if not self._matches_any(path, self.policy.allowed_paths)]
        if outside:
            reasons.append("path_not_allowed")
        if self.policy.require_tests_for_code and self._is_code_task(task) and not task.test_requirements:
            reasons.append("missing_test_requirements_for_code")

        approved = not reasons
        updated = task
        if approved and not task.forbidden_actions:
            updated = updated.model_copy(update={"forbidden_actions": DEFAULT_FORBIDDEN_ACTIONS})
            reasons.append("default_forbidden_actions_applied")
        updated = updated.model_copy(update={"approved": approved})
        updated = self._with_metadata(updated, approved, reasons)
        return approved, reasons or ["approved_by_policy"], details, updated

    def _details(self, task: AgentTask) -> dict[str, Any]:
        matched_allowed = {
            path: pattern
            for path in task.affected_files
            if (pattern := self._first_match(path, self.policy.allowed_paths)) is not None
        }
        blocked_matches = [
            {"path": path, "pattern": pattern}
            for path in task.affected_files
            for pattern in [self._first_match(path, self.policy.blocked_paths)]
            if pattern is not None
        ]
        disallowed = [path for path in task.affected_files if path not in matched_allowed]
        return {
            "affected_files": list(task.affected_files),
            "disallowed_paths": disallowed,
            "allowed_paths": list(self.policy.allowed_paths),
            "matched_allowed_paths": matched_allowed,
            "blocked_paths_matched": blocked_matches,
            "risk_score": task.risk_score,
            "max_risk_score": self.policy.max_risk_score,
            "risk_level": task.risk_level.value,
            "allowed_risk_levels": [RiskLevel.LOW.value, RiskLevel.MEDIUM.value],
            "task_type": task.task_type.value,
            "allowed_task_types": list(self.policy.allowed_task_types),
            "max_changed_files": self.policy.max_changed_files,
            "changed_files_count": len(task.affected_files),
            "require_tests_for_code": self.policy.require_tests_for_code,
            "test_requirements": list(task.test_requirements),
        }

    def _with_metadata(self, task: AgentTask, approved: bool, reasons: list[str]) -> AgentTask:
        metadata = {
            **task.metadata,
            "auto_approval": {
                "approved_by_policy": approved,
                "reasons": reasons,
                "policy_name": POLICY_NAME,
                "policy_version": POLICY_VERSION,
            },
        }
        return task.model_copy(update={"metadata": metadata})

    @staticmethod
    def _is_code_task(task: AgentTask) -> bool:
        return task.task_type not in {TaskType.DOCS, TaskType.TESTS}

    @staticmethod
    def _matches_any(path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/")
        return any(fnmatchcase(normalized, pattern) for pattern in patterns)

    @staticmethod
    def _first_match(path: str, patterns: list[str]) -> str | None:
        normalized = path.replace("\\", "/")
        for pattern in patterns:
            if fnmatchcase(normalized, pattern):
                return pattern
        return None
