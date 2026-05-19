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
from agentlab.tools.gitlab_tool import GitLabTool


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
        try:
            self.gitlab_tool.comment_mr(mr.iid or mr.mr_id, self._comment(task, implementation, functional_tests, build_security, quality_review, security_review, risk, diff_stats, gate))
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

        auto_merge_attempted = False
        auto_merge_succeeded = False
        if not gate.allowed:
            actions.append(FinalizationAction.BLOCKED)
            return MRFinalizationResult(
                status=ReportStatus.PASSED if not errors else ReportStatus.FAILED,
                actions=actions,
                mr=mr,
                comment_posted=comment_posted,
                labels_applied=labels_applied,
                errors=errors,
            )

        if self._can_auto_merge(gate, implementation, mr):
            auto_merge_attempted = True
            try:
                merged = self.gitlab_tool.merge_mr_guarded(mr.iid or mr.mr_id, squash=True)
                mr = merged
                actions.append(FinalizationAction.AUTO_MERGED)
                auto_merge_succeeded = True
            except Exception as exc:
                errors.append(f"auto merge failed: {exc}")
                actions.append(FinalizationAction.FAILED)
        else:
            actions.append(FinalizationAction.SKIPPED)

        return MRFinalizationResult(
            status=ReportStatus.FAILED if errors else ReportStatus.PASSED,
            actions=actions,
            mr=mr,
            auto_merge_attempted=auto_merge_attempted,
            auto_merge_succeeded=auto_merge_succeeded,
            comment_posted=comment_posted,
            labels_applied=labels_applied,
            skipped_reason=None if auto_merge_attempted else self._auto_merge_skip_reason(gate, implementation, mr),
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
            "### Policy Checks\n"
            + "\n".join(f"- `{name}`: `{passed}`" for name, passed in sorted(gate.policy_checks.items()))
        )
