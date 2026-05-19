from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import AgentTask, GateDecision, ImplementationReport, ReportStatus, TaskPlan
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
        self.last_gate_context = {}

    def plan(self) -> TaskPlan:
        return TaskPlan(tasks=[AgentTask(id="t1", title="Task", approved=True)])

    def run_task(self, task: AgentTask) -> ImplementationReport:
        return ImplementationReport(task_id=task.id, branch="agent/t1", status=ReportStatus.PASSED, pushed=False)

    def review_and_gate(self, task: AgentTask, *, direct_main_push: bool = False) -> GateDecision:
        return GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=5)

    def provenance(self):  # type: ignore[no-untyped-def]
        return None


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
    assert result["merge_request"]["status"] == "skipped"  # type: ignore[index]
    assert result["mr_finalization"]["status"] == "skipped"  # type: ignore[index]
    assert "mr_finalization_result" in orchestrator.artifacts.payloads
    assert "direct_main_push_result" in orchestrator.artifacts.payloads
