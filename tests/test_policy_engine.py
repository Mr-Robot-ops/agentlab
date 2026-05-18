from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    Finding,
    FindingSeverity,
    ReportStatus,
    ReviewReport,
    RiskAssessment,
    RiskLevel,
    TaskType,
    Verdict,
)
from agentlab.models import TestReport as AgentTestReport
from agentlab.policies.policy_engine import PolicyEngine


def config(**overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": Path("."),
        "workspace_root": Path(".runs"),
        "allowed_commands": ["python -m pytest"],
        "forbidden_commands": [],
        "protected_paths": ["infra/prod"],
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def inputs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "task": AgentTask(id="t1", title="Task", task_type=TaskType.BUGFIX, approved=True),
        "risk": RiskAssessment(score=10, level=RiskLevel.LOW),
        "diff_stats": DiffStats(changed_files=["src/app.py"], added_lines=5, deleted_lines=1),
        "functional_tests": AgentTestReport(status=ReportStatus.PASSED, passed=True),
        "build_security": BuildSecurityReport(status=ReportStatus.PASSED, passed=True),
        "quality_review": ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok"),
        "security_review": ReviewReport(
            reviewer="security_architecture",
            verdict=Verdict.APPROVED,
            summary="ok",
        ),
        "rollback_plan": "revert commit",
    }
    base.update(overrides)
    return base


def test_auto_merge_disabled_by_default_blocks() -> None:
    decision = PolicyEngine(config()).evaluate(**inputs())  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "auto merge is disabled" in decision.blockers


def test_all_checks_pass_when_auto_merge_enabled() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(**inputs())  # type: ignore[arg-type]
    assert decision.allowed is True


def test_direct_main_push_disabled_by_default_blocks() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(),
        direct_main_push=True,
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "direct main push is disabled" in decision.blockers


def test_protected_paths_block() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(diff_stats=DiffStats(changed_files=["infra/prod/main.tf"], touched_protected_paths=["infra/prod/main.tf"]))
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert any("protected paths" in blocker for blocker in decision.blockers)


def test_critical_findings_block() -> None:
    report = BuildSecurityReport(
        status=ReportStatus.PASSED,
        passed=True,
        findings=[Finding(tool="gitleaks", severity=FindingSeverity.CRITICAL, title="secret", blocked=True)],
    )
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(build_security=report)
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "critical or blocking security findings present" in decision.blockers


def test_missing_rollback_plan_blocks() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(rollback_plan=None)
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "rollback plan missing" in decision.blockers
