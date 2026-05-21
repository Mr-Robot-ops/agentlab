from __future__ import annotations

from enum import Enum

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
        if state != "opened":
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
        readme_only = MRFinalizer._readme_only(diff_stats.changed_files)
        failed_policy_checks = [name for name, passed in sorted(gate.policy_checks.items()) if not passed]
        blocker_items = MRFinalizer._blocker_lines(gate.blockers)
        result = MRFinalizer._result_label(
            gate=gate,
            auto_merge_succeeded=auto_merge_succeeded,
            skipped_reason=skipped_reason,
        )
        why = MRFinalizer._why_summary(gate.blockers, skipped_reason, auto_merge_succeeded)
        checks = MRFinalizer._checks_lines(
            functional_tests=functional_tests,
            build_security=build_security,
            quality_review=quality_review,
            security_review=security_review,
            gate=gate,
            pipeline_status=pipeline_status,
            readme_only=readme_only,
            failed_policy_checks=failed_policy_checks,
        )
        audit_lines = MRFinalizer._audit_lines(
            audit_id=audit_id,
            direct_main_note=direct_main_note,
            supply_chain_status=supply_chain_status,
            skipped_reason=skipped_reason,
            failed_policy_checks=failed_policy_checks,
        )
        return (
            "## AgentLab Gate Report\n\n"
            "### Summary\n"
            f"- Result: {result}\n"
            f"- Why: {why}\n"
            f"- Task: `{task.id}` - {task.title}\n"
            f"- Risk: {MRFinalizer._label(risk.level)} ({risk.score})\n"
            f"- Changed files: {MRFinalizer._inline_files(diff_stats.changed_files)}\n\n"
            "### Implementation\n"
            f"- Branch: `{implementation.branch}`\n"
            f"- Commit: `{implementation.commit_sha or '<none>'}`\n"
            f"- Pushed: {MRFinalizer._yes_no(implementation.pushed)}\n"
            f"- Merged: {MRFinalizer._yes_no(auto_merge_succeeded)}\n"
            f"- Added/deleted lines: +{diff_stats.added_lines} / -{diff_stats.deleted_lines}\n\n"
            "### Checks\n"
            f"{checks}\n\n"
            "### Blockers\n"
            f"{blocker_items}\n\n"
            "### Audit\n"
            f"{audit_lines}"
        )

    @staticmethod
    def _label(value: object) -> str:
        if isinstance(value, Enum):
            value = value.value
        text = str(value).strip() if value is not None else "unknown"
        return text.replace("_", " ").lower()

    @staticmethod
    def _yes_no(value: bool) -> str:
        return "yes" if value else "no"

    @staticmethod
    def _inline_files(paths: list[str]) -> str:
        if not paths:
            return "none"
        return ", ".join(f"`{path}`" for path in paths)

    @staticmethod
    def _readme_only(paths: list[str]) -> bool:
        if not paths:
            return False
        for path in paths:
            name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name != "readme" and not name.startswith("readme."):
                return False
        return True

    @staticmethod
    def _result_label(
        *,
        gate: GateDecision,
        auto_merge_succeeded: bool,
        skipped_reason: str | None,
    ) -> str:
        if auto_merge_succeeded:
            return "merged"
        if not gate.allowed:
            return "blocked"
        if skipped_reason:
            return "not merged"
        return MRFinalizer._label(gate.verdict)

    @staticmethod
    def _why_summary(blockers: list[str], skipped_reason: str | None, auto_merge_succeeded: bool) -> str:
        if auto_merge_succeeded:
            return "merged successfully"
        reasons = [MRFinalizer._human_reason(blocker) for blocker in blockers]
        if skipped_reason and skipped_reason != "gate is blocked":
            reasons.append(MRFinalizer._human_reason(skipped_reason))
        unique_reasons: list[str] = []
        for reason in reasons:
            if reason and reason not in unique_reasons:
                unique_reasons.append(reason)
        return "; ".join(unique_reasons) if unique_reasons else "no blockers"

    @staticmethod
    def _human_reason(reason: str) -> str:
        normalized = reason.strip()
        replacements = {
            "auto_merge_enabled is false": "auto merge is disabled",
            "quality review is not approved": "quality review changes requested",
            "security/architecture review is not approved": "security/architecture review changes requested",
        }
        return replacements.get(normalized, normalized.replace("_", " "))

    @staticmethod
    def _blocker_lines(blockers: list[str]) -> str:
        if not blockers:
            return "- None"
        return "\n".join(f"- {MRFinalizer._sentence(MRFinalizer._human_reason(blocker))}" for blocker in blockers)

    @staticmethod
    def _sentence(text: str) -> str:
        if not text:
            return text
        return text[0].upper() + text[1:]

    @staticmethod
    def _checks_lines(
        *,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        gate: GateDecision,
        pipeline_status: str | None,
        readme_only: bool,
        failed_policy_checks: list[str],
    ) -> str:
        lines = [
            f"- Functional tests: {MRFinalizer._functional_tests_label(functional_tests, readme_only)}",
            f"- Build/security: {MRFinalizer._label(build_security.status)}",
            f"- Quality review: {MRFinalizer._label(quality_review.verdict)}",
            f"- Security/architecture review: {MRFinalizer._label(security_review.verdict)}",
            f"- Pipeline: {MRFinalizer._pipeline_label(pipeline_status)}",
        ]
        docs_status = gate.check_statuses.get("docs_check")
        structure_status = gate.check_statuses.get("structure_evidence_check")
        if readme_only or docs_status is not None:
            lines.insert(0, f"- Docs check: {MRFinalizer._label(docs_status or 'skipped')}")
        if readme_only or structure_status is not None:
            insert_at = 1 if readme_only or docs_status is not None else 0
            lines.insert(insert_at, f"- Structure evidence: {MRFinalizer._label(structure_status or 'skipped')}")
        if failed_policy_checks:
            checks = ", ".join(f"`{name}`" for name in failed_policy_checks)
            lines.append(f"- Failed policy checks: {checks}")
        return "\n".join(lines)

    @staticmethod
    def _functional_tests_label(functional_tests: TestReport, readme_only: bool) -> str:
        status = MRFinalizer._label(functional_tests.status)
        if readme_only and status == "skipped":
            return "skipped because README-only"
        return status

    @staticmethod
    def _pipeline_label(pipeline_status: str | None) -> str:
        return MRFinalizer._label(pipeline_status or "not checked")

    @staticmethod
    def _audit_lines(
        *,
        audit_id: str | None,
        direct_main_note: str | None,
        supply_chain_status: str | None,
        skipped_reason: str | None,
        failed_policy_checks: list[str],
    ) -> str:
        lines = [
            f"- Run ID: `{audit_id or '<unknown>'}`",
            f"- Direct-main: {direct_main_note or 'not applicable for MR finalization'}",
            f"- Supply-chain: {supply_chain_status or 'not provided'}",
        ]
        if skipped_reason:
            lines.append(f"- Merge skipped reason: {MRFinalizer._human_reason(skipped_reason)}")
        if failed_policy_checks:
            lines.append("Full policy details are available in `gate_decision.json`.")
        else:
            lines.append("No failed policy checks. Full policy details are available in `gate_decision.json`.")
        return "\n".join(lines)
