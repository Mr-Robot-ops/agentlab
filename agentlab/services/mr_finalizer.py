from __future__ import annotations

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    FinalizationAction,
    GateDecision,
    ImplementationReport,
    MRFinalizationResult,
    MergeRequestInfo,
    ReportStatus,
    ReviewReport,
    RiskAssessment,
    TestReport,
)
from agentlab.tools.gitlab_tool import GitLabTool, MERGEABLE_DETAILED_STATUSES, MERGEABLE_STATUSES


WAIT_PIPELINE_STATUSES = {"pending", "running", "created", "preparing", "waiting_for_resource"}
BLOCK_PIPELINE_STATUSES = {"failed", "canceled", "skipped", "manual", "missing"}


class MRFinalizer:
    def __init__(self, config: AppConfig, gitlab_tool: GitLabTool) -> None:
        self.config = config
        self.gitlab_tool = gitlab_tool

    def finalize(
        self,
        *,
        task: AgentTask,
        implementation: ImplementationReport,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        risk: RiskAssessment,
        diff_stats: DiffStats,
        gate: GateDecision,
        mr: MergeRequestInfo | None,
        audit_id: str | None = None,
        supply_chain_status: str | None = None,
        direct_main_note: str | None = None,
    ) -> MRFinalizationResult:
        if mr is None:
            return MRFinalizationResult(
                status=ReportStatus.SKIPPED,
                actions=[FinalizationAction.SKIPPED],
                skipped_reason="no merge request available for finalization",
            )

        actions: list[FinalizationAction] = []
        errors: list[str] = []
        labels = self._labels(gate)
        pipeline_status: str | None = None
        pipeline_url: str | None = None
        auto_merge_attempted = False
        auto_merge_succeeded = False
        skipped_reason = self._auto_merge_skip_reason(gate, implementation, mr)

        if skipped_reason is None:
            readiness = self.gitlab_tool.get_mr_merge_readiness(mr.iid or mr.mr_id)
            skipped_reason = self._readiness_skip_reason(readiness)
        if skipped_reason is None:
            pipeline = self._pipeline_for_merge(mr)
            pipeline_status = str(pipeline.get("status", "missing"))
            pipeline_url = pipeline.get("web_url")
            skipped_reason = self._pipeline_skip_reason(pipeline_status)
        if skipped_reason is None:
            auto_merge_attempted = True
            try:
                merged = self.gitlab_tool.merge_mr_guarded(mr.iid or mr.mr_id, squash=True)
                mr = merged
                actions.append(FinalizationAction.AUTO_MERGED)
                auto_merge_succeeded = True
            except Exception as exc:
                errors.append(f"auto merge failed: {exc}")
                actions.append(FinalizationAction.FAILED)
        elif gate.allowed:
            actions.append(FinalizationAction.SKIPPED)

        try:
            self.gitlab_tool.comment_mr(
                mr.iid or mr.mr_id,
                self._comment(
                    task,
                    implementation,
                    functional_tests,
                    build_security,
                    quality_review,
                    security_review,
                    risk,
                    diff_stats,
                    gate,
                    pipeline_status=pipeline_status,
                    auto_merge_attempted=auto_merge_attempted,
                    auto_merge_succeeded=auto_merge_succeeded,
                    skipped_reason=skipped_reason,
                    audit_id=audit_id,
                    supply_chain_status=supply_chain_status,
                    direct_main_note=direct_main_note,
                ),
            )
            actions.append(FinalizationAction.COMMENTED)
            comment_posted = True
        except Exception as exc:
            errors.append(f"comment failed: {exc}")
            comment_posted = False

        labels_applied: list[str] = []
        if labels:
            try:
                updated = self.gitlab_tool.add_labels_to_mr(mr.iid or mr.mr_id, labels)
                labels_applied = updated.labels
                actions.append(FinalizationAction.LABELED)
                mr = updated
            except Exception as exc:
                errors.append(f"label update failed: {exc}")

        if not gate.allowed:
            actions.append(FinalizationAction.BLOCKED)
            return MRFinalizationResult(
                status=ReportStatus.PASSED if not errors else ReportStatus.FAILED,
                actions=actions,
                mr=mr,
                pipeline_status=pipeline_status,
                pipeline_url=pipeline_url,
                comment_posted=comment_posted,
                labels_applied=labels_applied,
                audit_id=audit_id,
                direct_main_note=direct_main_note,
                supply_chain_status=supply_chain_status,
                skipped_reason=skipped_reason,
                errors=errors,
            )

        return MRFinalizationResult(
            status=ReportStatus.FAILED if errors else ReportStatus.PASSED,
            actions=actions,
            mr=mr,
            pipeline_status=pipeline_status,
            pipeline_url=pipeline_url,
            auto_merge_attempted=auto_merge_attempted,
            auto_merge_succeeded=auto_merge_succeeded,
            comment_posted=comment_posted,
            labels_applied=labels_applied,
            audit_id=audit_id,
            direct_main_note=direct_main_note,
            supply_chain_status=supply_chain_status,
            skipped_reason=skipped_reason if not auto_merge_attempted or not auto_merge_succeeded else None,
            errors=errors,
        )

    def _can_auto_merge(self, gate: GateDecision, implementation: ImplementationReport, mr: MergeRequestInfo) -> bool:
        return (
            self.config.auto_merge_enabled
            and gate.allowed
            and gate.mode == "merge_request"
            and not gate.blockers
            and implementation.pushed
            and mr is not None
        )

    def _auto_merge_skip_reason(
        self,
        gate: GateDecision,
        implementation: ImplementationReport,
        mr: MergeRequestInfo | None,
    ) -> str | None:
        if not self.config.auto_merge_enabled:
            return "auto_merge_enabled is false"
        if not gate.allowed or gate.blockers:
            return "gate is blocked"
        if gate.mode != "merge_request":
            return "gate mode is not merge_request"
        if not implementation.pushed:
            return "implementation branch was not pushed"
        if mr is None:
            return "merge request is missing"
        return None

    @staticmethod
    def _readiness_skip_reason(readiness: dict[str, object]) -> str | None:
        state = readiness.get("state")
        if state and state != "opened":
            return f"MR state is not opened: {state}"
        if readiness.get("draft"):
            return "MR is draft"
        if readiness.get("has_conflicts"):
            return "MR has conflicts"
        detailed = readiness.get("detailed_merge_status")
        merge_status = readiness.get("merge_status")
        if detailed:
            return None if str(detailed) in MERGEABLE_DETAILED_STATUSES else f"MR detailed_merge_status is not mergeable: {detailed}"
        if merge_status:
            return None if str(merge_status) in MERGEABLE_STATUSES else f"MR merge_status is not mergeable: {merge_status}"
        return "MR mergeability is unknown"

    def _pipeline_for_merge(self, mr: MergeRequestInfo) -> dict[str, object]:
        pipeline = self.gitlab_tool.get_mr_pipeline_status(mr.iid or mr.mr_id)
        status = pipeline.get("status", "missing")
        if status in WAIT_PIPELINE_STATUSES:
            pipeline = self.gitlab_tool.wait_for_pipeline(mr_iid=mr.iid or mr.mr_id, timeout_seconds=600)
        return pipeline

    @staticmethod
    def _pipeline_skip_reason(status: str) -> str | None:
        if status == "success":
            return None
        if status in BLOCK_PIPELINE_STATUSES:
            return f"MR pipeline status is not mergeable: {status}"
        if status in WAIT_PIPELINE_STATUSES:
            return f"MR pipeline did not finish before timeout: {status}"
        return f"MR pipeline status is unknown or unsupported: {status}"

    @staticmethod
    def _labels(gate: GateDecision) -> list[str]:
        labels = ["agent/gate-allowed" if gate.allowed else "agent/gate-blocked"]
        if gate.risk_score >= 60:
            labels.append("risk/high")
        elif gate.risk_score >= 25:
            labels.append("risk/medium")
        else:
            labels.append("risk/low")
        return labels

    @staticmethod
    def _comment(
        task: AgentTask,
        implementation: ImplementationReport,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        risk: RiskAssessment,
        diff_stats: DiffStats,
        gate: GateDecision,
        *,
        pipeline_status: str | None,
        auto_merge_attempted: bool,
        auto_merge_succeeded: bool,
        skipped_reason: str | None,
        audit_id: str | None,
        supply_chain_status: str | None,
        direct_main_note: str | None,
    ) -> str:
        blockers = "\n".join(f"- {blocker}" for blocker in gate.blockers) or "- None"
        changed = "\n".join(f"- `{path}`" for path in diff_stats.changed_files) or "- None"
        return (
            "## AgentLab Gate Report\n\n"
            f"**Task:** `{task.id}` - {task.title}\n"
            f"**Gate:** `{gate.verdict}`\n"
            f"**Risk:** `{risk.score}` / `{risk.level}`\n\n"
            "### Implementation\n"
            f"- Branch: `{implementation.branch}`\n"
            f"- Commit: `{implementation.commit_sha or '<none>'}`\n"
            f"- Pushed: `{implementation.pushed}`\n\n"
            "### Tests\n"
            f"- Functional: `{functional_tests.status}`\n"
            f"- Build/Security: `{build_security.status}`\n\n"
            "### Reviews\n"
            f"- Quality: `{quality_review.verdict}`\n"
            f"- Security/Architecture: `{security_review.verdict}`\n\n"
            "### Diff\n"
            f"- Added lines: `{diff_stats.added_lines}`\n"
            f"- Deleted lines: `{diff_stats.deleted_lines}`\n"
            f"- Changed files:\n{changed}\n\n"
            "### Blockers\n"
            f"{blockers}\n\n"
            "### Integration\n"
            f"- Pipeline: `{pipeline_status or 'not checked'}`\n"
            f"- Auto-merge attempted: `{auto_merge_attempted}`\n"
            f"- Auto-merge succeeded: `{auto_merge_succeeded}`\n"
            f"- Auto-merge skipped reason: `{skipped_reason or '<none>'}`\n"
            f"- Direct-main: `{direct_main_note or 'not applicable for MR finalization'}`\n"
            f"- Supply-chain: `{supply_chain_status or 'not provided'}`\n"
            f"- Audit/Run ID: `{audit_id or '<unknown>'}`\n\n"
            "### Policy Checks\n"
            + "\n".join(f"- `{name}`: `{passed}`" for name, passed in sorted(gate.policy_checks.items()))
        )
