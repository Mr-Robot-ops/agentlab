from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    CommandResult,
    DiffStats,
    DocsCheckReport,
    Finding,
    FindingSeverity,
    ReportStatus,
    ReviewComment,
    ReviewReport,
    RiskAssessment,
    RiskLevel,
    SbomDocument,
    SupplyChainReport,
    TaskType,
    TestQualityFinding as QualityFinding,
    TestQualityReport as QualityReport,
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


def test_placeholder_test_quality_blocks_gate_even_when_other_checks_pass() -> None:
    report = QualityReport(
        status=ReportStatus.FAILED,
        passed=False,
        reason="placeholder_test_detected",
        findings=[
            QualityFinding(
                path="rust-backend/tests/smoke.rs",
                line=3,
                reason="assert_true",
                description="placeholder",
            )
        ],
    )

    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(test_quality=report)
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert decision.check_statuses["test_quality"] == "failed"
    assert decision.policy_checks["test_quality_passed"] is False
    assert "placeholder test detected" in decision.blockers
    assert "placeholder_test_detected" in decision.reasons


def test_direct_main_push_disabled_by_default_blocks() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(),
        direct_main_push=True,
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "direct main push is disabled" in decision.blockers


def test_direct_main_push_allowed_when_policy_enabled() -> None:
    decision = PolicyEngine(config(direct_main_push_enabled=True)).evaluate(
        **inputs(),
        direct_main_push=True,
    )  # type: ignore[arg-type]
    assert decision.allowed is True


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


def test_critical_quality_syntax_review_blocks_gate() -> None:
    quality = ReviewReport(
        reviewer="quality",
        verdict=Verdict.CHANGES_REQUESTED,
        summary="Rust test has a syntax error.",
        comments=[
            ReviewComment(
                path="rust-backend/tests/smoke.rs",
                line=4,
                body="Rust test function is missing a closing brace.",
                severity=FindingSeverity.CRITICAL,
            )
        ],
    )

    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(quality_review=quality)
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert decision.policy_checks["quality_review_approved"] is False
    assert "quality review is not approved" in decision.blockers


def test_missing_rollback_plan_blocks() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(rollback_plan=None)
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "rollback plan missing" in decision.blockers


def test_forbidden_task_type_blocks() -> None:
    task = AgentTask(id="infra", title="Infra", task_type=TaskType.INFRA, approved=True)
    decision = PolicyEngine(config(auto_merge_enabled=True, forbidden_task_types=["infra"])).evaluate(
        **inputs(task=task)
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "task type is forbidden by policy: infra" in decision.blockers


def test_required_test_command_must_execute() -> None:
    report = AgentTestReport(
        status=ReportStatus.PASSED,
        passed=True,
        commands=[CommandResult(command="python -m pytest", cwd=".", exit_code=0)],
    )
    decision = PolicyEngine(config(auto_merge_enabled=True, required_test_commands=["npm test"])).evaluate(
        **inputs(functional_tests=report)
    )  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "required test commands were not executed: npm test" in decision.blockers


def test_supply_chain_lockfile_policy_blocks_when_required() -> None:
    supply_chain = SupplyChainReport(
        status=ReportStatus.FAILED,
        passed=False,
        manifests=["pyproject.toml"],
        missing_lockfiles=["pyproject.toml"],
        components_count=0,
        sbom=SbomDocument(serialNumber="urn:uuid:test"),
    )
    decision = PolicyEngine(config(auto_merge_enabled=True, require_lockfiles_for_merge=True)).evaluate(
        **inputs(supply_chain=supply_chain)
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert "supply chain analysis did not pass" in decision.blockers
    assert "dependency lockfiles missing: pyproject.toml" in decision.blockers


def docs_report(
    *,
    docs_check: str = "passed",
    structure_evidence_check: str = "skipped",
    findings: list[Finding] | None = None,
) -> DocsCheckReport:
    return DocsCheckReport(
        status=ReportStatus.PASSED if docs_check == "passed" and structure_evidence_check != "failed" else ReportStatus.FAILED,
        passed=docs_check == "passed" and structure_evidence_check != "failed",
        checks={
            "docs_check": docs_check,
            "structure_evidence_check": structure_evidence_check,
        },
        findings=findings or [],
    )


def readme_task() -> AgentTask:
    return AgentTask(id="docs-readme", title="Docs README", task_type=TaskType.DOCS, approved=True, affected_files=["README.md"])


def skipped_tests() -> AgentTestReport:
    return AgentTestReport(status=ReportStatus.SKIPPED, passed=False, recommendation="README-only change.")


def test_readme_only_changes_do_not_require_functional_tests_without_required_commands() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(
            task=readme_task(),
            diff_stats=DiffStats(changed_files=["README.md"], added_lines=1),
            functional_tests=skipped_tests(),
        ),
        docs_check=docs_report(),
    )  # type: ignore[arg-type]

    assert decision.allowed is True
    assert "functional tests did not pass" not in decision.blockers
    assert decision.policy_checks["functional_tests_passed"] is True
    assert decision.check_statuses["docs_check"] == "passed"
    assert decision.check_statuses["structure_evidence_check"] == "skipped"


def test_readme_only_changes_still_require_configured_required_tests() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True, required_test_commands=["python -m pytest"])).evaluate(
        **inputs(
            task=readme_task(),
            diff_stats=DiffStats(changed_files=["README.md"], added_lines=1),
            functional_tests=skipped_tests(),
        ),
        docs_check=docs_report(),
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert "required test commands were not executed: python -m pytest" in decision.blockers
    assert "functional tests did not pass" in decision.blockers


def test_failed_docs_check_blocks_readme_only_change() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(
            task=readme_task(),
            diff_stats=DiffStats(changed_files=["README.md"], added_lines=1),
            functional_tests=skipped_tests(),
        ),
        docs_check=docs_report(docs_check="failed"),
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert "docs check failed" in decision.blockers
    assert decision.check_statuses["docs_check"] == "failed"


def test_failed_structure_evidence_blocks_readme_only_change() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(
            task=readme_task(),
            diff_stats=DiffStats(changed_files=["README.md"], added_lines=1),
            functional_tests=skipped_tests(),
        ),
        docs_check=docs_report(
            structure_evidence_check="failed",
            findings=[
                Finding(
                    tool="docs_check",
                    severity=FindingSeverity.HIGH,
                    title="README project structure removes existing files",
                    blocked=True,
                )
            ],
        ),
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert "project structure evidence failed" in decision.blockers
    assert "README project structure removes existing files" in decision.blockers
    assert decision.check_statuses["structure_evidence_check"] == "failed"


def test_broken_readme_docs_check_blocks_readme_only_change() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(
        **inputs(
            task=readme_task(),
            diff_stats=DiffStats(changed_files=["README.md"], added_lines=1),
            functional_tests=skipped_tests(),
        ),
        docs_check=docs_report(
            docs_check="failed",
            findings=[
                Finding(
                    tool="docs_check",
                    severity=FindingSeverity.HIGH,
                    title="Markdown fence is not closed",
                    blocked=True,
                )
            ],
        ),
    )  # type: ignore[arg-type]

    assert decision.allowed is False
    assert "docs check failed" in decision.blockers
    assert "Markdown fence is not closed" in decision.blockers


def test_non_docs_gate_decision_keeps_empty_check_statuses() -> None:
    decision = PolicyEngine(config(auto_merge_enabled=True)).evaluate(**inputs())  # type: ignore[arg-type]

    assert decision.allowed is True
    assert decision.check_statuses == {}
