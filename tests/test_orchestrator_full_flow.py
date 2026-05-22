from __future__ import annotations

from contextlib import nullcontext
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
    RiskLevel,
    TaskPlan,
    TaskType,
    TestReport as AgentTestReport,
    Verdict,
)
from agentlab.orchestrator import Orchestrator


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, **kwargs: object) -> None:
        self.events.append(kwargs)

    def span(self, **kwargs: object):
        self.events.append(kwargs)
        return nullcontext()


class FakeArtifacts:
    def __init__(self) -> None:
        self.payloads: dict[str, object] = {}

    def write_json(self, name: str, payload: object) -> None:
        self.payloads[name] = payload


class DummyOrchestrator(Orchestrator):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = False
        self.run_id = "run-1"
        self.audit = FakeAudit()
        self.artifacts = FakeArtifacts()
        self.last_gate_context = None

    def plan(self) -> TaskPlan:
        return TaskPlan(tasks=[AgentTask(id="t1", title="Task", approved=True)])

    def run_task(self, task: AgentTask) -> ImplementationReport:
        return ImplementationReport(task_id=task.id, branch="agent/t1", status=ReportStatus.PASSED, pushed=False)

    def review_and_gate(self, task: AgentTask, *, direct_main_push: bool = False) -> GateDecision:
        return GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=5)

    def provenance(self) -> None:
        return None


class AutoApproveOrchestrator(DummyOrchestrator):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.implemented_task: AgentTask | None = None

    def plan(self) -> TaskPlan:
        return TaskPlan(
            tasks=[
                AgentTask(
                    id="docs-readme",
                    title="Docs README",
                    task_type=TaskType.DOCS,
                    risk_level=RiskLevel.LOW,
                    risk_score=1,
                    affected_files=["README.md"],
                    forbidden_actions=["Do not change source code."],
                    approved=False,
                )
            ]
        )

    def run_task(self, task: AgentTask) -> ImplementationReport:
        self.implemented_task = task
        return ImplementationReport(task_id=task.id, branch="agent/docs-readme-run-1", status=ReportStatus.PASSED, pushed=True)


def test_full_flow_skips_auto_merge_when_no_mr_exists() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        auto_merge_enabled=True,
        push_agent_branches_enabled=False,
    )
    orchestrator = DummyOrchestrator(config)

    result = orchestrator.full_flow()

    assert result["status"] == "passed"
    merge_request = result["merge_request"]
    mr_finalization = result["mr_finalization"]
    assert isinstance(merge_request, dict)
    assert isinstance(mr_finalization, dict)
    assert merge_request["status"] == "skipped"
    assert mr_finalization["status"] == "skipped"
    assert "mr_finalization_result" in orchestrator.artifacts.payloads
    assert "direct_main_push_result" in orchestrator.artifacts.payloads


def test_full_flow_uses_auto_approved_task() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        push_agent_branches_enabled=False,
        auto_approve={"enabled": True},
    )
    orchestrator = AutoApproveOrchestrator(config)

    result = orchestrator.full_flow()

    assert result["status"] == "passed"
    assert orchestrator.implemented_task is not None
    assert orchestrator.implemented_task.approved is True
    assert orchestrator.implemented_task.metadata["auto_approval"]["approved_by_policy"] is True
    assert "auto_approval_report" in orchestrator.artifacts.payloads
    assert "approved_plan" in orchestrator.artifacts.payloads
    report = orchestrator.artifacts.payloads["auto_approval_report"]
    assert isinstance(report, dict)
    assert report["selected_task_id"] == "docs-readme"


def test_full_flow_with_task_id_uses_matching_approved_task() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        push_agent_branches_enabled=False,
        auto_approve={"enabled": True},
    )
    orchestrator = DummyOrchestrator(config)
    approved_plan = TaskPlan(
        tasks=[
            AgentTask(id="docs-01-credentials", title="Docs", approved=True),
            AgentTask(id="tests-02-smoke-baseline", title="Tests", approved=True),
        ]
    )

    result = orchestrator.full_flow(task_id="tests-02-smoke-baseline", approved_plan=approved_plan)

    assert result["status"] == "passed"
    assert result["selected_task_id"] == "tests-02-smoke-baseline"
    assert result["implementation"]["task_id"] == "tests-02-smoke-baseline"
    selected = orchestrator.artifacts.payloads["selected_task"]
    assert isinstance(selected, dict)
    assert selected["selected_task_id"] == "tests-02-smoke-baseline"
    assert selected["selection_mode"] == "requested"


def test_full_flow_with_unknown_task_id_does_not_fall_back() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        push_agent_branches_enabled=False,
        auto_approve={"enabled": True},
    )
    orchestrator = DummyOrchestrator(config)
    approved_plan = TaskPlan(tasks=[AgentTask(id="docs-01-credentials", title="Docs", approved=True)])

    result = orchestrator.full_flow(task_id="tests-02-smoke-baseline", approved_plan=approved_plan)

    assert result["status"] == "blocked"
    assert result["reason"] == "selected task not found in approved plan"
    assert result["selected_task_id"] == "tests-02-smoke-baseline"
    assert "implementation_report" not in orchestrator.artifacts.payloads


def test_full_flow_with_rejected_task_id_does_not_fall_back() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        push_agent_branches_enabled=False,
        auto_approve={"enabled": True},
    )
    orchestrator = DummyOrchestrator(config)
    approved_plan = TaskPlan(tasks=[AgentTask(id="tests-02-smoke-baseline", title="Tests", approved=False)])

    result = orchestrator.full_flow(task_id="tests-02-smoke-baseline", approved_plan=approved_plan)

    assert result["status"] == "blocked"
    assert result["reason"] == "selected task is not approved"
    assert result["selected_task_id"] == "tests-02-smoke-baseline"
    assert "implementation_report" not in orchestrator.artifacts.payloads


class FakeReadmeGit:
    def diff(self, base: str = "main") -> str:
        return "diff --git a/README.md b/README.md\n"

    def diff_stats(self, base: str = "main", protected_paths: list[str] | None = None) -> DiffStats:
        return DiffStats(changed_files=["README.md"], added_lines=1)


class FakeReadmeFileTool:
    def read_file(self, path: str) -> str:
        return "# Demo\n\nREADME update.\n"


class ReadmeOnlyGateOrchestrator(Orchestrator):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = False
        self.run_id = "run-1"
        self.audit = FakeAudit()
        self.artifacts = FakeArtifacts()
        self.ollama = object()

    def _tools(self):  # type: ignore[override]
        return FakeReadmeGit(), FakeReadmeFileTool(), object(), object()


def test_review_and_gate_skips_functional_tests_for_readme_only_without_required_commands(monkeypatch) -> None:
    class FailingFunctionalTestAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self):
            raise AssertionError("functional tests should be skipped for README-only changes")

    class PassingBuildSecurityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self) -> BuildSecurityReport:
            return BuildSecurityReport(status=ReportStatus.PASSED, passed=True)

    class ApprovedQualityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def review(self, diff_text: str) -> ReviewReport:
            return ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok")

    class ApprovedSecurityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def review(self, diff_text: str) -> ReviewReport:
            return ReviewReport(reviewer="security_architecture", verdict=Verdict.APPROVED, summary="ok")

    monkeypatch.setattr("agentlab.orchestrator.FunctionalTestAgent", FailingFunctionalTestAgent)
    monkeypatch.setattr("agentlab.orchestrator.BuildSecurityTestAgent", PassingBuildSecurityAgent)
    monkeypatch.setattr("agentlab.orchestrator.CodeQualityReviewAgent", ApprovedQualityAgent)
    monkeypatch.setattr("agentlab.orchestrator.SecurityArchitectureReviewAgent", ApprovedSecurityAgent)
    cfg = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        auto_merge_enabled=True,
        supply_chain_enabled=False,
    )
    orchestrator = ReadmeOnlyGateOrchestrator(cfg)

    decision = orchestrator.review_and_gate(
        AgentTask(id="docs-readme", title="Docs README", task_type=TaskType.DOCS, affected_files=["README.md"], approved=True)
    )

    assert decision.allowed is True
    assert decision.check_statuses["docs_check"] == "passed"
    assert "docs_check_report" in orchestrator.artifacts.payloads
    docs_report = orchestrator.artifacts.payloads["docs_check_report"]
    assert getattr(docs_report, "docs_check") == "passed"
    assert getattr(docs_report, "structure_evidence_check") == "skipped"
    functional = orchestrator.artifacts.payloads["functional_test_report"]
    assert getattr(functional, "status") == ReportStatus.SKIPPED


class FakeRustGit:
    def diff(self, base: str = "main") -> str:
        return "diff --git a/rust-backend/tests/smoke.rs b/rust-backend/tests/smoke.rs\n"

    def diff_stats(self, base: str = "main", protected_paths: list[str] | None = None) -> DiffStats:
        return DiffStats(changed_files=["rust-backend/tests/smoke.rs"], added_lines=4)


class FakeRustFileTool:
    def list_files(self) -> list[str]:
        return ["rust-backend/Cargo.toml", "rust-backend/tests/smoke.rs"]

    def read_file(self, path: str) -> str:
        if path == "rust-backend/Cargo.toml":
            return '[package]\nname = "rust-backend"\nversion = "0.1.0"\nedition = "2021"\n'
        if path == "rust-backend/tests/smoke.rs":
            return "#[test]\nfn test_smoke() {\n    assert!(true);\n}\n"
        raise FileNotFoundError(path)


class RustPlaceholderGateOrchestrator(Orchestrator):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = False
        self.run_id = "run-1"
        self.audit = FakeAudit()
        self.artifacts = FakeArtifacts()
        self.ollama = object()

    def _tools(self):  # type: ignore[override]
        return FakeRustGit(), FakeRustFileTool(), object(), object()


def test_review_and_gate_writes_test_quality_report_and_blocks_placeholder_tests(monkeypatch) -> None:
    class PassingFunctionalTestAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self):
            return AgentTestReport(status=ReportStatus.PASSED, passed=True)

    class PassingBuildSecurityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self) -> BuildSecurityReport:
            return BuildSecurityReport(status=ReportStatus.PASSED, passed=True)

    class ApprovedQualityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def review(self, diff_text: str) -> ReviewReport:
            return ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok")

    class ApprovedSecurityAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def review(self, diff_text: str) -> ReviewReport:
            return ReviewReport(reviewer="security_architecture", verdict=Verdict.APPROVED, summary="ok")

    monkeypatch.setattr("agentlab.orchestrator.FunctionalTestAgent", PassingFunctionalTestAgent)
    monkeypatch.setattr("agentlab.orchestrator.BuildSecurityTestAgent", PassingBuildSecurityAgent)
    monkeypatch.setattr("agentlab.orchestrator.CodeQualityReviewAgent", ApprovedQualityAgent)
    monkeypatch.setattr("agentlab.orchestrator.SecurityArchitectureReviewAgent", ApprovedSecurityAgent)
    cfg = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        auto_merge_enabled=True,
        supply_chain_enabled=False,
    )
    orchestrator = RustPlaceholderGateOrchestrator(cfg)

    decision = orchestrator.review_and_gate(
        AgentTask(
            id="tests-02-smoke-baseline",
            title="Add smoke baseline",
            task_type=TaskType.TESTS,
            affected_files=["rust-backend/tests/smoke.rs"],
            approved=True,
        )
    )

    assert decision.allowed is False
    assert decision.check_statuses["test_quality"] == "failed"
    assert "placeholder test detected" in decision.blockers
    report = orchestrator.artifacts.payloads["test_quality_report"]
    assert getattr(report, "reason") == "placeholder_test_detected"
    assert getattr(report, "findings")[0].path == "rust-backend/tests/smoke.rs"
