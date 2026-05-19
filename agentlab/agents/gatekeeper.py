from __future__ import annotations

from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    DiffStats,
    GateDecision,
    ReviewReport,
    RiskAssessment,
    SupplyChainReport,
    TestReport,
)
from agentlab.policies.policy_engine import PolicyEngine


class Gatekeeper:
    name = "gatekeeper"

    def __init__(self, policy_engine: PolicyEngine) -> None:
        self.policy_engine = policy_engine

    def decide(
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
        supply_chain: SupplyChainReport | None = None,
        direct_main_push: bool = False,
    ) -> GateDecision:
        return self.policy_engine.evaluate(
            task=task,
            risk=risk,
            diff_stats=diff_stats,
            functional_tests=functional_tests,
            build_security=build_security,
            quality_review=quality_review,
            security_review=security_review,
            supply_chain=supply_chain,
            rollback_plan=rollback_plan,
            direct_main_push=direct_main_push,
        )
