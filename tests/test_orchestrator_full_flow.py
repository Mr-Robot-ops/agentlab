from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import AgentTask, GateDecision, ImplementationReport, ReportStatus, RiskLevel, TaskPlan, TaskType
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
