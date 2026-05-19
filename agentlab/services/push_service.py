from __future__ import annotations

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    DirectMainPushResult,
    GateDecision,
    ImplementationReport,
    ReportStatus,
    ReviewReport,
    TestReport,
    Verdict,
)
from agentlab.tools.git_tool import GitTool
from agentlab.tools.test_tool import TestTool


class PushService:
    def __init__(self, config: AppConfig, git_tool: GitTool, test_tool: TestTool, *, dry_run: bool = False) -> None:
        self.config = config
        self.git_tool = git_tool
        self.test_tool = test_tool
        self.dry_run = dry_run

    def push_direct_main(
        self,
        *,
        task: AgentTask,
        implementation: ImplementationReport,
        gate: GateDecision,
        diff_stats: DiffStats,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        rollback_plan: str | None,
        audit_id: str,
    ) -> DirectMainPushResult:
        errors = self._preflight_errors(
            implementation=implementation,
            gate=gate,
            diff_stats=diff_stats,
            functional_tests=functional_tests,
            build_security=build_security,
            quality_review=quality_review,
            security_review=security_review,
            rollback_plan=rollback_plan,
        )
        if errors:
            return DirectMainPushResult(status=ReportStatus.SKIPPED, errors=errors, skipped_reason="; ".join(errors))

        actions: list[str] = []
        if self.dry_run:
            return DirectMainPushResult(status=ReportStatus.SKIPPED, actions=["dry-run"], skipped_reason="dry-run is active")

        try:
            if self.git_tool.status_porcelain():
                raise RuntimeError("workspace is dirty before direct-main push")
            self.git_tool.checkout(self.config.default_branch)
            actions.append(f"checkout {self.config.default_branch}")
            pull = self.git_tool.pull_ff_only(branch=self.config.default_branch)
            if not pull.ok:
                raise RuntimeError(pull.stderr or "git pull --ff-only failed")
            actions.append("pull --ff-only")
            cherry_pick = self.git_tool.cherry_pick(implementation.commit_sha or "", no_commit=True, allow_default_branch=True)
            if not cherry_pick.ok:
                raise RuntimeError(cherry_pick.stderr or "git cherry-pick --no-commit failed")
            actions.append(f"cherry-pick {implementation.commit_sha}")
            final_stats = self.git_tool.diff_stats("HEAD", self.config.protected_paths)
            final_errors = self._diff_policy_errors(gate, final_stats)
            if final_errors:
                raise RuntimeError("; ".join(final_errors))
            message = self._commit_message(task, gate, audit_id)
            commit_sha = self.git_tool.commit_direct_main(message)
            if not commit_sha:
                raise RuntimeError("no direct-main changes to commit")
            actions.append(f"commit {commit_sha}")
            self._run_required_tests()
            if self.git_tool.status_porcelain():
                raise RuntimeError("workspace became dirty after required tests")
            push = self.git_tool.push_default_branch()
            if not push.ok:
                raise RuntimeError(push.stderr or "git push default branch failed")
            actions.append("push default branch")
            return DirectMainPushResult(
                status=ReportStatus.PASSED,
                pushed=True,
                branch=self.config.default_branch,
                commit_sha=commit_sha,
                actions=actions,
            )
        except Exception as exc:
            return DirectMainPushResult(
                status=ReportStatus.FAILED,
                branch=self.config.default_branch,
                actions=actions,
                errors=[str(exc)],
            )

    def _preflight_errors(
        self,
        *,
        implementation: ImplementationReport,
        gate: GateDecision,
        diff_stats: DiffStats,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        rollback_plan: str | None,
    ) -> list[str]:
        errors: list[str] = []
        if not self.config.direct_main_push_enabled:
            errors.append("direct_main_push_enabled is false")
        if not gate.allowed or gate.mode != "direct_main_push" or gate.blockers:
            errors.append("gate does not allow direct_main_push")
        if gate.risk_score > self.config.max_risk_score_for_direct_main_push:
            errors.append("risk score exceeds direct-main limit")
        errors.extend(self._diff_policy_errors(gate, diff_stats))
        if functional_tests.status != ReportStatus.PASSED or not functional_tests.passed:
            errors.append("functional tests did not pass")
        if build_security.status != ReportStatus.PASSED or not build_security.passed:
            errors.append("build/security tests did not pass")
        if quality_review.verdict != Verdict.APPROVED:
            errors.append("quality review is not approved")
        if security_review.verdict != Verdict.APPROVED:
            errors.append("security/architecture review is not approved")
        if not rollback_plan:
            errors.append("rollback plan missing")
        if not implementation.commit_sha:
            errors.append("implementation commit is missing")
        return errors

    def _diff_policy_errors(self, gate: GateDecision, diff_stats: DiffStats) -> list[str]:
        errors = []
        if diff_stats.touched_protected_paths:
            errors.append("protected paths touched: " + ", ".join(diff_stats.touched_protected_paths))
        if diff_stats.secrets_touched:
            errors.append("secrets touched")
        if len(diff_stats.changed_files) > self.config.max_changed_files:
            errors.append("too many changed files")
        if diff_stats.added_lines > self.config.max_added_lines:
            errors.append("too many added lines")
        if diff_stats.deleted_lines > self.config.max_deleted_lines:
            errors.append("too many deleted lines")
        if gate.risk_score > self.config.max_risk_score_for_direct_main_push:
            errors.append("risk score exceeds direct-main limit")
        return errors

    def _run_required_tests(self) -> None:
        for command in self.config.required_test_commands:
            result = self.test_tool.run_command(command)
            if not result.ok:
                raise RuntimeError(f"required test failed before direct-main push: {command}")

    @staticmethod
    def _commit_message(task: AgentTask, gate: GateDecision, audit_id: str) -> str:
        return (
            f"agent: {task.title}\n\n"
            f"Task-ID: {task.id}\n"
            f"Audit-ID: {audit_id}\n"
            f"Risk-Score: {gate.risk_score}\n"
            f"Gate-Verdict: {gate.verdict}\n"
        )
