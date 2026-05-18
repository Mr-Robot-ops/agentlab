import pytest
from pydantic import ValidationError

from agentlab.models import AgentTask, GateDecision, PatchProposal, ReviewReport, TaskType, Verdict


def test_task_rejects_unsafe_id() -> None:
    with pytest.raises(ValidationError):
        AgentTask(id="../bad", title="Bad")


def test_task_accepts_structured_fields() -> None:
    task = AgentTask(
        id="safe-1",
        title="Safe",
        task_type=TaskType.BUGFIX,
        acceptance_criteria=["passes tests"],
        affected_files=["src/app.py"],
        approved=True,
    )
    assert task.id == "safe-1"
    assert task.task_type == TaskType.BUGFIX


def test_patch_proposal_requires_rollback() -> None:
    with pytest.raises(ValidationError):
        PatchProposal(task_id="safe-1", summary="x", patch="diff --git a/x b/x\n", affected_files=["x"])


def test_review_report_rejects_unknown_verdict() -> None:
    with pytest.raises(ValidationError):
        ReviewReport(reviewer="quality", verdict="maybe", summary="nope")  # type: ignore[arg-type]


def test_gate_decision_serializes_to_json_values() -> None:
    decision = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=99,
        blockers=["risk"],
    )
    assert decision.model_dump(mode="json")["verdict"] == "blocked"
    assert Verdict.APPROVED.value == "approved"
