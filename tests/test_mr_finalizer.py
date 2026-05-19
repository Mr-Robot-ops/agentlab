from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    GateDecision,
    ImplementationReport,
    MergeRequestInfo,
    ReportStatus,
    ReviewReport,
    RiskAssessment,
    RiskLevel,
    Verdict,
)
from agentlab.models import TestReport as AgentTestReport
from agentlab.services.mr_finalizer import MRFinalizer


class FakeGitLabTool:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: list[str] = []
        self.merge_called = False

    def comment_mr(self, mr_id: int, body: str) -> None:
        self.comments.append((mr_id, body))

    def add_labels_to_mr(self, mr_id: int, labels: list[str]) -> MergeRequestInfo:
        self.labels.extend(labels)
        return MergeRequestInfo(mr_id=1, iid=mr_id, title="MR", source_branch="agent/t1", target_branch="main", labels=labels)

    def merge_mr_guarded(self, mr_id: int, *, squash: bool = True) -> MergeRequestInfo:
        self.merge_called = True
        return MergeRequestInfo(mr_id=1, iid=mr_id, title="MR", source_branch="agent/t1", target_branch="main")


def config(**overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": Path("."),
        "workspace_root": Path(".runs"),
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def inputs(gate: GateDecision) -> dict[str, object]:
    return {
        "task": AgentTask(id="t1", title="Task", approved=True),
        "implementation": ImplementationReport(task_id="t1", branch="agent/t1", status=ReportStatus.PASSED, pushed=True),
        "functional_tests": AgentTestReport(status=ReportStatus.PASSED, passed=True),
        "build_security": BuildSecurityReport(status=ReportStatus.PASSED, passed=True),
        "quality_review": ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok"),
        "security_review": ReviewReport(reviewer="security_architecture", verdict=Verdict.APPROVED, summary="ok"),
        "risk": RiskAssessment(score=10, level=RiskLevel.LOW),
        "diff_stats": DiffStats(changed_files=["src/app.py"], added_lines=1),
        "gate": gate,
        "mr": MergeRequestInfo(mr_id=1, iid=7, title="MR", source_branch="agent/t1", target_branch="main"),
    }


def test_finalizer_comments_but_does_not_merge_blocked_gate() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=10,
        blockers=["functional tests did not pass"],
    )

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.comment_posted is True
    assert fake.comments
    assert fake.merge_called is False
    assert result.auto_merge_attempted is False


def test_finalizer_auto_merges_only_when_gate_allows() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.auto_merge_attempted is True
    assert result.auto_merge_succeeded is True
    assert fake.merge_called is True
