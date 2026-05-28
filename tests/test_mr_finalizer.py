from __future__ import annotations

from pathlib import Path

import pytest

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
    def __init__(
        self,
        *,
        pipeline_status: str = "success",
        wait_status: str | None = None,
        readiness: dict[str, object] | None = None,
    ) -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: list[str] = []
        self.merge_called = False
        self.wait_called = False
        self.pipeline_status = pipeline_status
        self.wait_status = wait_status
        self.readiness = readiness or {
            "state": "opened",
            "draft": False,
            "has_conflicts": False,
            "detailed_merge_status": "mergeable",
            "merge_status": "can_be_merged",
        }

    def comment_mr(self, mr_id: int, body: str) -> None:
        self.comments.append((mr_id, body))

    def add_labels_to_mr(self, mr_id: int, labels: list[str]) -> MergeRequestInfo:
        self.labels.extend(labels)
        return MergeRequestInfo(mr_id=1, iid=mr_id, title="MR", source_branch="agent/t1", target_branch="main", labels=labels)

    def merge_mr_guarded(self, mr_id: int, *, squash: bool = True) -> MergeRequestInfo:
        self.merge_called = True
        return MergeRequestInfo(mr_id=1, iid=mr_id, title="MR", source_branch="agent/t1", target_branch="main")

    def get_mr_merge_readiness(self, mr_id: int) -> dict[str, object]:
        return self.readiness

    def get_mr_pipeline_status(self, mr_id: int) -> dict[str, object]:
        return {"status": self.pipeline_status, "web_url": "https://gitlab.example.com/pipeline/1"}

    def wait_for_pipeline(self, *, mr_iid: int, timeout_seconds: int = 600) -> dict[str, object]:
        self.wait_called = True
        return {"status": self.wait_status or self.pipeline_status, "web_url": "https://gitlab.example.com/pipeline/2"}


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
    assert result.pipeline_status == "success"


def test_finalizer_waits_for_running_pipeline_before_merge() -> None:
    fake = FakeGitLabTool(pipeline_status="running", wait_status="success")
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert fake.wait_called is True
    assert result.auto_merge_succeeded is True
    assert result.pipeline_status == "success"


def test_finalizer_blocks_auto_merge_when_pipeline_failed_and_comments_reason() -> None:
    fake = FakeGitLabTool(pipeline_status="failed")
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.auto_merge_attempted is False
    assert fake.merge_called is False
    assert result.pipeline_status == "failed"
    assert result.skipped_reason == "MR pipeline status is not mergeable: failed"
    assert "MR pipeline status is not mergeable: failed" in fake.comments[-1][1]


@pytest.mark.parametrize("pipeline_status", ["missing", "manual", "canceled", "skipped"])
def test_finalizer_blocks_auto_merge_for_non_success_terminal_pipeline_states(pipeline_status: str) -> None:
    fake = FakeGitLabTool(pipeline_status=pipeline_status)
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.auto_merge_attempted is False
    assert fake.merge_called is False
    assert result.pipeline_status == pipeline_status
    assert result.skipped_reason == f"MR pipeline status is not mergeable: {pipeline_status}"


def test_finalizer_blocks_auto_merge_when_waited_pipeline_does_not_finish() -> None:
    fake = FakeGitLabTool(pipeline_status="running", wait_status="running")
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert fake.wait_called is True
    assert result.auto_merge_attempted is False
    assert fake.merge_called is False
    assert result.pipeline_status == "running"
    assert result.skipped_reason == "MR pipeline did not finish before timeout: running"


def test_finalizer_blocks_auto_merge_when_mr_is_draft() -> None:
    fake = FakeGitLabTool(readiness={"state": "opened", "draft": True, "has_conflicts": False})
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.auto_merge_attempted is False
    assert result.skipped_reason == "MR is draft"
    assert fake.merge_called is False


def test_finalizer_blocks_auto_merge_when_mr_state_is_unknown() -> None:
    fake = FakeGitLabTool(
        readiness={
            "state": None,
            "draft": False,
            "has_conflicts": False,
            "detailed_merge_status": "mergeable",
            "merge_status": "can_be_merged",
        }
    )
    gate = GateDecision(allowed=True, mode="merge_request", verdict="allowed", risk_score=10)

    result = MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    assert result.auto_merge_attempted is False
    assert result.skipped_reason == "MR state is not opened: None"
    assert fake.merge_called is False


def test_finalizer_comment_is_reviewer_readable_for_readme_only_blocked_gate() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=10,
        blockers=[
            "auto merge is disabled",
            "quality review is not approved",
            "security/architecture review is not approved",
        ],
        policy_checks={
            "mode_enabled": False,
            "quality_review_approved": False,
            "security_review_approved": False,
            "functional_tests_passed": True,
        },
        check_statuses={"docs_check": "passed", "structure_evidence_check": "skipped"},
    )
    payload = inputs(gate)
    payload["functional_tests"] = AgentTestReport(
        status=ReportStatus.SKIPPED,
        passed=True,
        recommendation="README-only change: docs checks are authoritative.",
    )
    payload["quality_review"] = ReviewReport(
        reviewer="quality",
        verdict=Verdict.CHANGES_REQUESTED,
        summary="needs attention",
    )
    payload["security_review"] = ReviewReport(
        reviewer="security_architecture",
        verdict=Verdict.CHANGES_REQUESTED,
        summary="needs attention",
    )
    payload["diff_stats"] = DiffStats(changed_files=["README.md"], added_lines=2, deleted_lines=1)

    MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**payload)  # type: ignore[arg-type]

    comment = fake.comments[-1][1]
    assert "ReportStatus." not in comment
    assert "Verdict." not in comment
    assert "RiskLevel." not in comment
    assert "### Summary" in comment
    assert "- Result: blocked" in comment
    assert "- Why: auto merge is disabled; quality review changes requested; security/architecture review changes requested" in comment
    assert "- Risk: low (10)" in comment
    assert "- Changed files: `README.md`" in comment
    assert "- Docs check: passed" in comment
    assert "- Structure evidence: skipped" in comment
    assert "- Functional tests: skipped because README-only" in comment
    assert "- Quality review: changes requested" in comment
    assert "- Security/architecture review: changes requested" in comment
    assert "- Pipeline: not checked" in comment
    assert "Full policy details are available in `gate_decision.json`." in comment


def test_finalizer_comment_omits_passing_policy_checks_and_shows_failed_checks() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=10,
        blockers=["failing policy check"],
        policy_checks={
            "passing_check": True,
            "failing_check": False,
        },
    )

    MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    comment = fake.comments[-1][1]
    assert "`failing_check`" in comment
    assert "passing_check" not in comment


def test_finalizer_comment_includes_test_quality_failure() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=10,
        blockers=["placeholder test detected"],
        policy_checks={
            "test_quality_passed": False,
            "functional_tests_passed": True,
        },
        check_statuses={"test_quality": "failed"},
    )

    MRFinalizer(config(auto_merge_enabled=True), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    comment = fake.comments[-1][1]
    assert "- Test quality: failed" in comment
    assert "- Placeholder test detected" in comment
    assert "`test_quality_passed`" in comment


def test_finalizer_comment_includes_test_quality_warning() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=True,
        mode="merge_request",
        verdict="allowed",
        risk_score=10,
        policy_checks={
            "test_quality_passed": True,
            "functional_tests_passed": True,
        },
        check_statuses={"test_quality": "warning"},
    )

    MRFinalizer(config(auto_merge_enabled=False), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    comment = fake.comments[-1][1]
    assert "- Test quality: warning" in comment
    assert "`test_quality_passed`" not in comment


def test_finalizer_comment_mentions_when_no_policy_checks_failed() -> None:
    fake = FakeGitLabTool()
    gate = GateDecision(
        allowed=True,
        mode="merge_request",
        verdict="allowed",
        risk_score=10,
        policy_checks={"mode_enabled": True, "functional_tests_passed": True},
    )

    MRFinalizer(config(auto_merge_enabled=False), fake).finalize(**inputs(gate))  # type: ignore[arg-type]

    comment = fake.comments[-1][1]
    assert "No failed policy checks. Full policy details are available in `gate_decision.json`." in comment
    assert "mode_enabled" not in comment
    assert "functional_tests_passed" not in comment
