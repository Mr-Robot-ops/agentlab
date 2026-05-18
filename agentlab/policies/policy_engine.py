from __future__ import annotations

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    FindingSeverity,
    GateDecision,
    ReportStatus,
    ReviewReport,
    RiskAssessment,
    TestReport,
    Verdict,
)


class PolicyEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        task: AgentTask,
        risk: RiskAssessment,
        diff_stats: DiffStats,
        functional_tests: TestReport,
        build_security: BuildSecurityReport,
        quality_review: ReviewReport,
        security_review: ReviewReport,
        rollback_plan: str | None,
        direct_main_push: bool = False,
    ) -> GateDecision:
        mode = "direct_main_push" if direct_main_push else "merge_request"
        checks: dict[str, bool] = {}
        blockers: list[str] = []
        reasons: list[str] = []

        self._check(
            checks,
            blockers,
            "mode_enabled",
            self.config.direct_main_push_enabled if direct_main_push else self.config.auto_merge_enabled,
            "direct main push is disabled" if direct_main_push else "auto merge is disabled",
        )
        task_type = task.task_type.value
        self._check(
            checks,
            blockers,
            "task_type_allowed",
            not self.config.allowed_task_types or task_type in self.config.allowed_task_types,
            f"task type is not allowed by policy: {task_type}",
        )
        self._check(
            checks,
            blockers,
            "task_type_not_forbidden",
            task_type not in self.config.forbidden_task_types,
            f"task type is forbidden by policy: {task_type}",
        )
        self._check(checks, blockers, "risk_not_blocked", not risk.blocked, "risk assessment is blocked")

        risk_limit = (
            self.config.max_risk_score_for_direct_main_push
            if direct_main_push
            else self.config.max_risk_score_for_merge
        )
        self._check(
            checks,
            blockers,
            "risk_score_under_limit",
            risk.score <= risk_limit,
            f"risk score {risk.score} exceeds limit {risk_limit}",
        )
        self._check(
            checks,
            blockers,
            "changed_files_under_limit",
            len(diff_stats.changed_files) <= self.config.max_changed_files,
            "too many changed files",
        )
        self._check(
            checks,
            blockers,
            "added_lines_under_limit",
            diff_stats.added_lines <= self.config.max_added_lines,
            "too many added lines",
        )
        self._check(
            checks,
            blockers,
            "deleted_lines_under_limit",
            diff_stats.deleted_lines <= self.config.max_deleted_lines,
            "too many deleted lines",
        )
        self._check(
            checks,
            blockers,
            "no_protected_paths",
            not diff_stats.touched_protected_paths,
            "protected paths touched: " + ", ".join(diff_stats.touched_protected_paths),
        )
        self._check(checks, blockers, "no_secrets", not diff_stats.secrets_touched, "secrets touched")

        if self.config.require_two_testers:
            executed_commands = {result.command for result in functional_tests.commands}
            missing_required_tests = [
                command for command in self.config.required_test_commands if command not in executed_commands
            ]
            self._check(
                checks,
                blockers,
                "required_tests_executed",
                not missing_required_tests,
                "required test commands were not executed: " + ", ".join(missing_required_tests),
            )
            self._check(
                checks,
                blockers,
                "functional_tests_passed",
                functional_tests.status == ReportStatus.PASSED and functional_tests.passed,
                "functional tests did not pass",
            )
            self._check(
                checks,
                blockers,
                "build_security_tests_passed",
                build_security.status == ReportStatus.PASSED and build_security.passed,
                "build/security tests did not pass",
            )

        if self.config.require_two_reviewers:
            self._check(
                checks,
                blockers,
                "quality_review_approved",
                quality_review.verdict == Verdict.APPROVED,
                "quality review is not approved",
            )
            self._check(
                checks,
                blockers,
                "security_review_approved",
                security_review.verdict == Verdict.APPROVED,
                "security/architecture review is not approved",
            )

        critical_findings = [
            finding
            for finding in build_security.findings
            if finding.blocked or finding.severity == FindingSeverity.CRITICAL
        ]
        self._check(
            checks,
            blockers,
            "no_critical_security_findings",
            not critical_findings,
            "critical or blocking security findings present",
        )
        self._check(checks, blockers, "rollback_plan_present", bool(rollback_plan), "rollback plan missing")

        if not blockers:
            reasons.append("all deterministic policy checks passed")
        reasons.extend(risk.reasons)

        allowed = not blockers
        return GateDecision(
            allowed=allowed,
            mode=mode,  # type: ignore[arg-type]
            verdict="allowed" if allowed else "blocked",
            risk_score=risk.score,
            reasons=reasons,
            blockers=blockers,
            policy_checks=checks,
        )

    @staticmethod
    def _check(
        checks: dict[str, bool],
        blockers: list[str],
        name: str,
        passed: bool,
        blocker: str,
    ) -> None:
        checks[name] = passed
        if not passed:
            blockers.append(blocker)
