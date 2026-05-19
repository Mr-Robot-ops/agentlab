from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    GateDecision,
    ImplementationReport,
    ReportStatus,
    ReviewReport,
    Verdict,
)
from agentlab.models import TestReport as AgentTestReport
from agentlab.services.push_service import PushService


class FakeGitTool:
    pass


class FakeTestTool:
    pass


def config(**overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": Path("."),
        "workspace_root": Path(".runs"),
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def inputs(diff_stats: DiffStats | None = None) -> dict[str, object]:
    return {
        "task": AgentTask(id="t1", title="Task", approved=True),
        "implementation": ImplementationReport(
            task_id="t1",
            branch="agent/t1",
            status=ReportStatus.PASSED,
            commit_sha="abc123",
        ),
        "gate": GateDecision(allowed=True, mode="direct_main_push", verdict="allowed", risk_score=5),
        "diff_stats": diff_stats or DiffStats(changed_files=["src/app.py"], added_lines=1),
        "functional_tests": AgentTestReport(status=ReportStatus.PASSED, passed=True),
        "build_security": BuildSecurityReport(status=ReportStatus.PASSED, passed=True),
        "quality_review": ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok"),
        "security_review": ReviewReport(reviewer="security_architecture", verdict=Verdict.APPROVED, summary="ok"),
        "rollback_plan": "revert commit",
        "audit_id": "run-1",
    }


def test_push_service_refuses_when_direct_push_disabled() -> None:
    result = PushService(config(), FakeGitTool(), FakeTestTool()).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert "direct_main_push_enabled is false" in result.errors


def test_push_service_refuses_protected_paths() -> None:
    result = PushService(
        config(direct_main_push_enabled=True),
        FakeGitTool(),
        FakeTestTool(),
    ).push_direct_main(
        **inputs(DiffStats(changed_files=["infra/prod/main.tf"], touched_protected_paths=["infra/prod/main.tf"]))
    )  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert any("protected paths touched" in error for error in result.errors)


def test_push_service_refuses_secrets_touched() -> None:
    result = PushService(
        config(direct_main_push_enabled=True),
        FakeGitTool(),
        FakeTestTool(),
    ).push_direct_main(
        **inputs(DiffStats(changed_files=["secrets/api.env"], secrets_touched=True))
    )  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert "secrets touched" in result.errors
