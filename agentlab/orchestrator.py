from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.agents.docs_check import DocsCheckAgent, is_readme_only
from agentlab.agents.gatekeeper import Gatekeeper
from agentlab.agents.implementer import ImplementationAgent
from agentlab.agents.mr_agent import MergeRequestAgent
from agentlab.agents.planner import PlanningAgent
from agentlab.agents.review_quality import CodeQualityReviewAgent
from agentlab.agents.review_security_architecture import SecurityArchitectureReviewAgent
from agentlab.agents.rollback import RollbackRecoveryAgent
from agentlab.agents.test_build_security import BuildSecurityTestAgent
from agentlab.agents.test_functional import FunctionalTestAgent
from agentlab.audit import AuditLogger
from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    ArchitectureSummary,
    DirectMainPushResult,
    GateContext,
    GateDecision,
    ImplementationReport,
    MergeRequestInfo,
    MRFinalizationResult,
    ProvenanceStatement,
    RepoIndex,
    ReportStatus,
    RiskLevel,
    RollbackReport,
    PostMergeMonitorResult,
    StructuredEditProposal,
    StewardReport,
    SupplyChainReport,
    TaskPlan,
    TaskType,
    TestReport,
)
from agentlab.preflight import PreflightChecker
from agentlab.policies.auto_approval import AutoApprovalPolicy
from agentlab.policies.policy_engine import PolicyEngine
from agentlab.policies.risk import assess_risk
from agentlab.provenance import ProvenanceBuilder
from agentlab.repo_indexer import RepoIndexer
from agentlab.repo_policy import apply_repo_policy, load_repo_policy
from agentlab.services.mr_finalizer import MRFinalizer
from agentlab.services.push_service import PushService
from agentlab.steward import BacklogSteward
from agentlab.supply_chain import SupplyChainAnalyzer
from agentlab.tools.docker_tool import DockerTool
from agentlab.tools.file_tool import FileTool
from agentlab.tools.git_tool import GitTool
from agentlab.tools.gitlab_tool import GitLabTool
from agentlab.tools.ollama_client import OllamaClient
from agentlab.tools.test_tool import TestTool
from agentlab.workspace import WorkspaceManager


class Orchestrator:
    def __init__(self, config: AppConfig, *, dry_run: bool = False, run_id: str | None = None) -> None:
        self.config = config
        self.dry_run = dry_run
        self.run_id = run_id or uuid.uuid4().hex
        self.run_dir = Path(config.workspace_root) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLogger(self.run_dir / config.audit_file, self.run_id)
        with self.audit.span(agent="workspace", action="prepare"):
            self.workspace_info = WorkspaceManager(config, self.audit).prepare()
        self.repo_policy = load_repo_policy(config.target_repo_path, config.repo_policy_file)
        self.config = apply_repo_policy(config, self.repo_policy)
        self.artifacts = ArtifactStore(self.run_dir, self.run_id)
        if self.repo_policy is not None:
            self.artifacts.write_json("repo_policy", self.repo_policy)
        self.ollama = OllamaClient(config.ollama, timeout_seconds=config.command_timeout_seconds)
        self.repo_index: RepoIndex | None = None
        self.architecture_summary: ArchitectureSummary | None = None
        self.supply_chain_report: SupplyChainReport | None = None
        self.last_gate_context: GateContext | None = None

    def _tools(self) -> tuple[GitTool, FileTool, TestTool, DockerTool]:
        repo = self.config.target_repo_path
        git_tool = GitTool(
            repo,
            default_branch=self.config.default_branch,
            timeout_seconds=self.config.command_timeout_seconds,
            audit=self.audit,
            dry_run=self.dry_run,
        )
        file_tool = FileTool(repo, self.config, dry_run=self.dry_run)
        test_tool = TestTool(repo, self.config)
        docker_tool = DockerTool(repo, self.config)
        return git_tool, file_tool, test_tool, docker_tool

    def plan(self) -> TaskPlan:
        self.preflight("plan", enforce=False)
        repo_index, architecture = self.index_repository()
        if self.config.supply_chain_enabled:
            self.supply_chain()
        _, file_tool, _, _ = self._tools()
        with self.audit.span(agent="planner", action="plan"):
            result = PlanningAgent(self.config, file_tool, self.ollama, repo_index=repo_index, architecture=architecture).plan()
        self.artifacts.write_json("plan", result)
        return result

    def run_task(self, task: AgentTask) -> ImplementationReport:
        self.preflight("run-task", enforce=True)
        repo_index, architecture = self.index_repository()
        supply_chain = self.supply_chain() if self.config.supply_chain_enabled else None
        git_tool, file_tool, _, _ = self._tools()
        start = time.monotonic()
        self.audit.emit(agent="implementer", action="implement", status="started", input_payload=task.model_dump(mode="json"))
        result = ImplementationAgent(
            self.config,
            git_tool,
            file_tool,
            self.ollama,
            dry_run=self.dry_run,
            repo_context={
                "architecture": architecture.model_dump(mode="json"),
                "repo_index_summary": self._repo_index_summary(repo_index),
                "supply_chain_summary": self._supply_chain_summary(supply_chain) if supply_chain else None,
            },
            artifacts=self.artifacts,
            run_id=self.run_id,
        ).implement(task)
        self.audit.emit(
            agent="implementer",
            action="implement",
            status="succeeded" if result.status == ReportStatus.PASSED else "failed",
            duration_seconds=time.monotonic() - start,
            metadata={
                "implementation_status": result.status.value,
                "implementation_error_count": len(result.errors),
                "failure_stage": result.failure_stage,
                "failure_reason": result.failure_reason,
            },
            error="; ".join(result.errors) if result.status != ReportStatus.PASSED and result.errors else None,
            output_payload=result.model_dump(mode="json"),
        )
        self.artifacts.write_json("implementation_report", result)
        return result

    def index_repository(self) -> tuple[RepoIndex, ArchitectureSummary]:
        if self.repo_index is not None and self.architecture_summary is not None:
            return self.repo_index, self.architecture_summary
        self.preflight("index", enforce=False)
        with self.audit.span(agent="repo_indexer", action="index_repository"):
            indexer = RepoIndexer(self.config)
            self.repo_index = indexer.build_index()
            self.architecture_summary = indexer.summarize_architecture(self.repo_index)
        self.artifacts.write_json("repo_index", self.repo_index)
        self.artifacts.write_json("architecture_summary", self.architecture_summary)
        return self.repo_index, self.architecture_summary

    def supply_chain(self) -> SupplyChainReport:
        if self.supply_chain_report is not None:
            return self.supply_chain_report
        repo_index, _ = self.index_repository()
        with self.audit.span(agent="supply_chain", action="analyze"):
            self.supply_chain_report = SupplyChainAnalyzer(self.config, repo_index).analyze()
        self.artifacts.write_json("supply_chain_report", self.supply_chain_report)
        self.artifacts.write_json("sbom_cyclonedx", self.supply_chain_report.sbom)
        return self.supply_chain_report

    def provenance(self) -> ProvenanceStatement:
        with self.audit.span(agent="provenance", action="build_statement"):
            statement = ProvenanceBuilder(
                self.config,
                run_id=self.run_id,
                run_dir=self.run_dir,
                artifacts=self.artifacts,
            ).build()
        self.artifacts.write_json("run_provenance", statement)
        return statement

    def steward(self) -> StewardReport:
        repo_index, architecture = self.index_repository()
        if self.config.supply_chain_enabled:
            self.supply_chain()
        with self.audit.span(agent="steward", action="build_backlog"):
            report = BacklogSteward(repo_index, architecture).build_report()
        self.artifacts.write_json("steward_report", report)
        self.artifacts.write_json("backlog", [item.proposed_task for item in report.backlog])
        return report

    @staticmethod
    def _repo_index_summary(index: RepoIndex) -> dict[str, object]:
        return {
            "total_files": index.total_files,
            "indexed_files": index.indexed_files,
            "languages": index.languages,
            "top_level_dirs": index.top_level_dirs,
            "manifests": index.manifests,
            "test_files": index.test_files[:100],
            "docs_files": index.docs_files[:50],
            "ci_files": index.ci_files,
            "docker_files": index.docker_files,
            "kubernetes_files": index.kubernetes_files[:100],
            "infra_files": index.infra_files[:100],
            "security_files": index.security_files[:100],
            "entrypoint_candidates": index.entrypoint_candidates,
            "todos": [todo.model_dump(mode="json") for todo in index.todos[:50]],
            "warnings": index.warnings,
        }

    @staticmethod
    def _supply_chain_summary(report: SupplyChainReport) -> dict[str, object]:
        return {
            "status": report.status,
            "passed": report.passed,
            "components_count": report.components_count,
            "package_managers": report.package_managers,
            "missing_lockfiles": report.missing_lockfiles,
            "findings": [finding.model_dump(mode="json") for finding in report.findings[:50]],
            "recommendations": report.recommendations,
        }

    def preflight(self, mode: str, *, enforce: bool = True) -> object:
        with self.audit.span(agent="preflight", action=mode):
            report = PreflightChecker(self.config, mode=mode).run()
        self.artifacts.write_json(f"preflight_{mode.replace('-', '_')}", report)
        if enforce and not report.passed:
            failed = [check.name for check in report.checks if check.status == "failed"]
            self.audit.emit(
                agent="preflight",
                action=mode,
                status="blocked",
                metadata={"failed_checks": failed},
                output_payload=report.model_dump(mode="json"),
            )
            raise RuntimeError("preflight failed: " + ", ".join(failed))
        return report

    def full_flow(
        self,
        *,
        task_id: str | None = None,
        approved_plan: TaskPlan | None = None,
        auto_approval_report: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        self.audit.emit(agent="orchestrator", action="full_flow", status="started", metadata={"selected_task_id": task_id})
        try:
            if approved_plan is None:
                plan = self.plan()
                approved_plan, auto_approval_report = AutoApprovalPolicy(self.config).apply(plan)
            else:
                auto_approval_report = auto_approval_report or _auto_approval_report_from_approved_plan(approved_plan, task_id)
            self.artifacts.write_json("auto_approval_report", auto_approval_report)
            if self.config.auto_approve.enabled:
                self.artifacts.write_json("approved_plan", approved_plan)
            if task_id is not None:
                matching = [task for task in approved_plan.tasks if task.id == task_id]
                if not matching:
                    result = {
                        "run_id": self.run_id,
                        "status": "blocked",
                        "reason": "selected task not found in approved plan",
                        "selected_task_id": task_id,
                        "plan": approved_plan.model_dump(mode="json"),
                        "auto_approval": auto_approval_report,
                    }
                    self.audit.emit(
                        agent="orchestrator",
                        action="full_flow",
                        status="blocked",
                        metadata={"reason": result["reason"], "selected_task_id": task_id},
                        output_payload=result,
                    )
                    return result
                task = matching[0]
                if not task.approved:
                    result = {
                        "run_id": self.run_id,
                        "status": "blocked",
                        "reason": "selected task is not approved",
                        "selected_task_id": task_id,
                        "plan": approved_plan.model_dump(mode="json"),
                        "auto_approval": auto_approval_report,
                    }
                    self.audit.emit(
                        agent="orchestrator",
                        action="full_flow",
                        status="blocked",
                        metadata={"reason": result["reason"], "selected_task_id": task_id},
                        output_payload=result,
                    )
                    return result
            approved_tasks = [task for task in approved_plan.tasks if task.approved]
            if not approved_tasks:
                result = {
                    "run_id": self.run_id,
                    "status": "blocked",
                    "reason": "no approved task available for implementation",
                    "selected_task_id": task_id,
                    "plan": approved_plan.model_dump(mode="json"),
                    "auto_approval": auto_approval_report,
                }
                self.audit.emit(
                    agent="orchestrator",
                    action="full_flow",
                    status="blocked",
                    metadata={"reason": result["reason"]},
                    output_payload=result,
                )
                return result
            if task_id is None and self.config.auto_approve.enabled:
                task = AutoApprovalPolicy.select_task(approved_tasks)
                assert task is not None
            elif task_id is None:
                task = approved_tasks[0]
            self.artifacts.write_json(
                "selected_task",
                {
                    "selected_task_id": task.id,
                    "selection_mode": "requested" if task_id is not None else "auto",
                },
            )
            implementation = self.run_task(task)
            if implementation.status != ReportStatus.PASSED:
                result = {"run_id": self.run_id, "status": "failed", "selected_task_id": task.id, "implementation": implementation.model_dump(mode="json")}
                self.audit.emit(
                    agent="orchestrator",
                    action="full_flow",
                    status="failed",
                    metadata={"reason": "implementation failed"},
                    output_payload=result,
                )
                return result
            mr_result: dict[str, object]
            mr_info = None
            gitlab_tool = None
            if self.config.push_agent_branches_enabled and not self.dry_run:
                try:
                    gitlab_tool = GitLabTool(self.config)
                    with self.audit.span(agent="mr_agent", action="create_or_update_mr"):
                        mr_info = MergeRequestAgent(self.config, gitlab_tool).create_or_update(
                            task=task,
                            implementation=implementation,
                        )
                    mr_result = {"status": "created", "mr": mr_info.model_dump(mode="json")}
                except Exception as exc:
                    mr_result = {"status": "failed", "error": str(exc)}
            else:
                self.audit.emit(
                    agent="mr_agent",
                    action="create_or_update_mr",
                    status="skipped",
                    metadata={"reason": "push_agent_branches_enabled is false or dry-run is active"},
                )
                mr_result = {"status": "skipped", "reason": "push_agent_branches_enabled is false or dry-run is active"}
            direct_main_mode = self.config.direct_main_push_enabled and not self.config.push_agent_branches_enabled
            decision = self.review_and_gate(task, direct_main_push=direct_main_mode)
            finalization = self._finalize_mr(task, implementation, decision, mr_info, gitlab_tool)
            direct_push = (
                self._direct_main_push(task, implementation, decision)
                if direct_main_mode
                else self._skipped_direct_main_push("direct_main_push_enabled is false or MR flow is active")
            )
            post_merge = self._post_merge_monitor(gitlab_tool, mr_info, finalization, direct_push)
            provenance = self.provenance() if self.config.provenance_enabled else None
            result = {
                "run_id": self.run_id,
                "status": "passed" if decision.allowed else "blocked",
                "selected_task_id": task.id,
                "implementation": implementation.model_dump(mode="json"),
                "merge_request": mr_result,
                "gate": decision.model_dump(mode="json"),
                "mr_finalization": finalization.model_dump(mode="json"),
                "direct_main_push": direct_push.model_dump(mode="json") if direct_push else None,
                "post_merge_monitor": post_merge.model_dump(mode="json"),
                "provenance": provenance.model_dump(mode="json") if provenance else None,
            }
            self.audit.emit(
                agent="orchestrator",
                action="full_flow",
                status="succeeded" if decision.allowed else "blocked",
                metadata={"gate_verdict": decision.verdict, "blockers": decision.blockers},
                output_payload=result,
            )
            return result
        except Exception as exc:
            self.audit.emit(agent="orchestrator", action="full_flow", status="failed", error=str(exc))
            raise

    def review_and_gate(self, task: AgentTask, *, direct_main_push: bool = False) -> GateDecision:
        git_tool, file_tool, test_tool, docker_tool = self._tools()
        base_ref = self.config.default_branch
        diff_text = git_tool.diff(base_ref)
        diff_stats = git_tool.diff_stats(base_ref, self.config.protected_paths)
        risk = assess_risk(task, diff_stats.changed_files, diff_text)
        supply_chain = self.supply_chain() if self.config.supply_chain_enabled else None
        readme_only = is_readme_only(diff_stats.changed_files)
        docs_check = None
        if readme_only:
            with self.audit.span(agent="docs_check", action="run_docs_checks"):
                docs_check = DocsCheckAgent(file_tool, self.artifacts).run(diff_stats.changed_files)
            self.artifacts.write_json("docs_check_report", docs_check)
        if readme_only and not self.config.required_test_commands:
            with self.audit.span(agent="functional_test", action="skip_tests_for_readme_only"):
                functional = TestReport(
                    status=ReportStatus.SKIPPED,
                    passed=False,
                    recommendation="README-only change: docs_check_report is authoritative; functional tests are skipped unless required_test_commands are configured.",
                )
        else:
            with self.audit.span(agent="functional_test", action="run_tests"):
                functional = FunctionalTestAgent(file_tool, test_tool).run()
        self.artifacts.write_json("functional_test_report", functional)
        with self.audit.span(agent="build_security_test", action="run_build_and_security_checks"):
            build_security = BuildSecurityTestAgent(
                file_tool,
                docker_tool,
                test_tool,
                docker_build_enabled=self.config.docker_build_enabled,
                docker_compose_enabled=self.config.docker_compose_enabled,
            ).run()
        self.artifacts.write_json("build_security_report", build_security)
        with self.audit.span(agent="review_quality", action="review_diff"):
            quality_review = CodeQualityReviewAgent(self.config, self.ollama).review(diff_text)
        self.artifacts.write_json("quality_review", quality_review)
        with self.audit.span(agent="review_security_architecture", action="review_diff"):
            security_review = SecurityArchitectureReviewAgent(self.config, self.ollama).review(diff_text)
        self.artifacts.write_json("security_architecture_review", security_review)
        rollback_plan = f"Revert the agent branch commit for task {task.id} or close the MR before merge."
        gatekeeper = Gatekeeper(PolicyEngine(self.config))
        with self.audit.span(agent="gatekeeper", action="decide"):
            decision = gatekeeper.decide(
                task=task,
                risk=risk,
                diff_stats=diff_stats,
                functional_tests=functional,
                build_security=build_security,
                quality_review=quality_review,
                security_review=security_review,
                supply_chain=supply_chain,
                docs_check=docs_check,
                rollback_plan=rollback_plan,
                direct_main_push=direct_main_push,
            )
        self.artifacts.write_json("risk_assessment", risk)
        self.artifacts.write_json("diff_stats", diff_stats)
        self.artifacts.write_json("gate_decision", decision)
        self.last_gate_context = GateContext(
            risk=risk,
            diff_stats=diff_stats,
            functional_tests=functional,
            build_security=build_security,
            quality_review=quality_review,
            security_review=security_review,
            rollback_plan=rollback_plan,
            supply_chain=supply_chain,
            docs_check=docs_check,
        )
        return decision

    def _finalize_mr(
        self,
        task: AgentTask,
        implementation: ImplementationReport,
        decision: GateDecision,
        mr_info: MergeRequestInfo | None,
        gitlab_tool: GitLabTool | None,
    ) -> MRFinalizationResult:
        if gitlab_tool is None or mr_info is None:
            result = MRFinalizationResult(
                status=ReportStatus.SKIPPED,
                skipped_reason="no merge request available; auto-merge skipped",
            )
            self.artifacts.write_json("mr_finalization_result", result)
            return result
        context = self._gate_context()
        with self.audit.span(agent="mr_finalizer", action="finalize"):
            result = MRFinalizer(self.config, gitlab_tool).finalize(
                task=task,
                implementation=implementation,
                functional_tests=context.functional_tests,
                build_security=context.build_security,
                quality_review=context.quality_review,
                security_review=context.security_review,
                risk=context.risk,
                diff_stats=context.diff_stats,
                gate=decision,
                mr=mr_info,
                audit_id=self.run_id,
                supply_chain_status=context.supply_chain.status.value if context.supply_chain else None,
                direct_main_note="direct-main disabled or irrelevant for merge-request mode",
            )
        self.artifacts.write_json("mr_finalization_result", result)
        return result

    def _direct_main_push(
        self,
        task: AgentTask,
        implementation: ImplementationReport,
        decision: GateDecision,
    ) -> DirectMainPushResult:
        git_tool, _, test_tool, _ = self._tools()
        context = self._gate_context()
        with self.audit.span(agent="push_service", action="direct_main_push"):
            result = PushService(self.config, git_tool, test_tool, dry_run=self.dry_run).push_direct_main(
                task=task,
                implementation=implementation,
                gate=decision,
                diff_stats=context.diff_stats,
                functional_tests=context.functional_tests,
                build_security=context.build_security,
                quality_review=context.quality_review,
                security_review=context.security_review,
                rollback_plan=context.rollback_plan,
                audit_id=self.run_id,
            )
        self.artifacts.write_json("direct_main_push_result", result)
        return result

    def _skipped_direct_main_push(self, reason: str) -> DirectMainPushResult:
        result = DirectMainPushResult(status=ReportStatus.SKIPPED, skipped_reason=reason)
        self.artifacts.write_json("direct_main_push_result", result)
        return result

    def _post_merge_monitor(
        self,
        gitlab_tool: GitLabTool | None,
        mr_info: MergeRequestInfo | None,
        finalization: MRFinalizationResult,
        direct_push: DirectMainPushResult | None,
    ) -> PostMergeMonitorResult:
        if finalization.auto_merge_succeeded and gitlab_tool is not None and mr_info is not None:
            with self.audit.span(agent="post_merge_monitor", action="wait_for_default_branch_pipeline"):
                pipeline = gitlab_tool.wait_for_pipeline(ref=self.config.default_branch, timeout_seconds=300)
            result = PostMergeMonitorResult(
                status=ReportStatus.PASSED if pipeline.get("status") == "success" else ReportStatus.FAILED,
                ref=self.config.default_branch,
                pipeline_status=pipeline.get("status"),
                pipeline_url=pipeline.get("web_url"),
                recommendation="Proceed" if pipeline.get("status") == "success" else "Inspect failed pipeline and run recovery.",
            )
            if result.status == ReportStatus.FAILED:
                result = result.model_copy(update={"recovery": self._recovery_report(gitlab_tool, ref=self.config.default_branch)})
        elif direct_push is not None and direct_push.pushed:
            if gitlab_tool is None:
                try:
                    gitlab_tool = GitLabTool(self.config)
                except Exception as exc:
                    result = PostMergeMonitorResult(
                        status=ReportStatus.SKIPPED,
                        ref=self.config.default_branch,
                        recommendation=f"Direct push completed, but GitLab pipeline monitoring could not start: {exc}",
                    )
                    self.artifacts.write_json("post_merge_monitor", result)
                    return result
            with self.audit.span(agent="post_merge_monitor", action="wait_for_default_branch_pipeline"):
                pipeline = gitlab_tool.wait_for_pipeline(ref=self.config.default_branch, timeout_seconds=300)
            result = PostMergeMonitorResult(
                status=ReportStatus.PASSED if pipeline.get("status") == "success" else ReportStatus.FAILED,
                ref=self.config.default_branch,
                pipeline_status=pipeline.get("status"),
                pipeline_url=pipeline.get("web_url"),
                recommendation="Proceed" if pipeline.get("status") == "success" else "Inspect failed pipeline and run recovery.",
            )
            if result.status == ReportStatus.FAILED:
                result = result.model_copy(update={"recovery": self._recovery_report(gitlab_tool, ref=self.config.default_branch)})
        else:
            result = PostMergeMonitorResult(status=ReportStatus.SKIPPED, recommendation="No merge or direct push was performed.")
        self.artifacts.write_json("post_merge_monitor", result)
        return result

    def _gate_context(self) -> GateContext:
        if self.last_gate_context is None:
            raise RuntimeError("gate context is not available")
        return self.last_gate_context

    def _recovery_report(self, gitlab_tool: GitLabTool, *, ref: str | None = None) -> RollbackReport:
        git_tool, _, _, _ = self._tools()
        with self.audit.span(agent="rollback", action="post_merge_recover"):
            report = RollbackRecoveryAgent(self.config, git_tool, gitlab_tool).recover(ref=ref)
        self.artifacts.write_json("recovery_report", report)
        return report

    def review_existing_mr(self, mr_id: int) -> dict[str, object]:
        gitlab_tool = GitLabTool(self.config)
        mr = gitlab_tool.project.mergerequests.get(mr_id)
        changes = mr.changes()
        diff_text = "\n".join(change.get("diff", "") for change in changes.get("changes", []))
        quality_review = CodeQualityReviewAgent(self.config, self.ollama).review(diff_text)
        security_review = SecurityArchitectureReviewAgent(self.config, self.ollama).review(diff_text)
        result = {
            "mr_id": mr_id,
            "quality_review": quality_review.model_dump(mode="json"),
            "security_review": security_review.model_dump(mode="json"),
        }
        self.artifacts.write_json("review_existing_mr", result)
        return result

    def recover(self, *, ref: str | None = None, commit_sha: str | None = None) -> dict[str, object]:
        git_tool, _, _, _ = self._tools()
        gitlab_tool = GitLabTool(self.config)
        report = RollbackRecoveryAgent(self.config, git_tool, gitlab_tool).recover(ref=ref, commit_sha=commit_sha)
        result = {"run_id": self.run_id, "recovery": report.model_dump(mode="json")}
        self.artifacts.write_json("recovery_report", report)
        return result

    def revise_existing_mr(
        self,
        *,
        mr_iid: int,
        source_branch: str,
        command: str,
        feedback: str,
        note_id: int | str,
        changed_files: list[str] | None = None,
        propose_only: bool = False,
    ) -> dict[str, object]:
        if not source_branch.startswith("agent/"):
            raise RuntimeError("revision source_branch must be an agent/* branch")
        git_tool, file_tool, _, _ = self._tools()
        if git_tool.status_porcelain():
            raise RuntimeError("workspace is dirty before MR revision")
        git_tool.fetch(ref=self.config.default_branch)
        checkout = git_tool.checkout_agent_branch(source_branch)
        if not checkout.ok:
            raise RuntimeError(checkout.stderr or f"could not checkout {source_branch}")

        base_ref = f"origin/{self.config.default_branch}"
        if changed_files:
            affected_files = changed_files
        else:
            try:
                affected_files = git_tool.changed_files(base_ref)
            except Exception:
                affected_files = git_tool.changed_files(self.config.default_branch)
        revision_context = self._build_revision_context(
            git_tool,
            file_tool,
            mr_iid=mr_iid,
            source_branch=source_branch,
            base_ref=base_ref,
            command=command,
            feedback=feedback,
            changed_files=affected_files,
        )
        task = self._review_comment_task(
            mr_iid=mr_iid,
            source_branch=source_branch,
            command=command,
            feedback=feedback,
            note_id=note_id,
            affected_files=affected_files,
            revision_context=revision_context,
        )
        self.artifacts.write_json("revision_task", task)

        plan = TaskPlan(summary=f"MR comment command /agent {command}", tasks=[task])
        approved_plan, auto_approval_report = AutoApprovalPolicy(self.config).apply(plan)
        self.artifacts.write_json("auto_approval_report", auto_approval_report)
        approved_task = approved_plan.tasks[0]
        if not approved_task.approved:
            return {
                "run_id": self.run_id,
                "status": "failed",
                "reason": "policy_blocked",
                "source_branch": source_branch,
                "command": command,
                "propose_only": propose_only,
                "task": task.model_dump(mode="json"),
                "changed_files": affected_files,
                "auto_approval": auto_approval_report,
            }

        repo_index, architecture = self.index_repository()
        supply_chain = self.supply_chain() if self.config.supply_chain_enabled else None
        implementation_agent = ImplementationAgent(
            self.config,
            git_tool,
            file_tool,
            self.ollama,
            dry_run=self.dry_run,
            repo_context={
                "architecture": architecture.model_dump(mode="json"),
                "repo_index_summary": self._repo_index_summary(repo_index),
                "supply_chain_summary": self._supply_chain_summary(supply_chain) if supply_chain else None,
                "revision_context": revision_context,
            },
            artifacts=self.artifacts,
            run_id=self.run_id,
        )
        if propose_only:
            implementation = implementation_agent.propose_on_branch(approved_task, source_branch)
            self.artifacts.write_json("implementation_report", implementation)
            cleanup = self._ensure_proposal_worktree_clean(git_tool, implementation.changed_files)
            if not cleanup["clean"]:
                return {
                    "run_id": self.run_id,
                    "status": "failed",
                    "reason": "proposal_cleanup_failed",
                    "source_branch": source_branch,
                    "command": command,
                    "propose_only": True,
                    "implementation": implementation.model_dump(mode="json"),
                    "auto_approval": auto_approval_report,
                    "cleanup": cleanup,
                }
            if implementation.status != ReportStatus.PASSED:
                return {
                    "run_id": self.run_id,
                    "status": "failed",
                    "reason": "proposal_failed",
                    "source_branch": source_branch,
                    "command": command,
                    "propose_only": True,
                    "implementation": implementation.model_dump(mode="json"),
                    "auto_approval": auto_approval_report,
                    "changed_files": implementation.changed_files,
                    "proposal_artifacts": _proposal_artifacts_from_report(implementation),
                }
            proposal_validation = self._validate_proposal(approved_task, implementation, file_tool)
            return {
                "run_id": self.run_id,
                "status": "passed",
                "reason": "proposal_generated",
                "source_branch": source_branch,
                "command": command,
                "propose_only": True,
                "commit_sha": None,
                "changed_files": implementation.changed_files,
                "implementation": implementation.model_dump(mode="json"),
                "auto_approval": auto_approval_report,
                "proposal_validation": proposal_validation,
                "proposal_artifacts": _proposal_artifacts_from_report(implementation),
            }

        implementation = implementation_agent.revise_on_branch(approved_task, source_branch)
        self.artifacts.write_json("implementation_report", implementation)
        if implementation.status != ReportStatus.PASSED:
            return {
                "run_id": self.run_id,
                "status": "failed",
                "reason": "revision_failed",
                "source_branch": source_branch,
                "command": command,
                "implementation": implementation.model_dump(mode="json"),
                "auto_approval": auto_approval_report,
            }

        gate = self.review_and_gate(approved_task, direct_main_push=False)
        return {
            "run_id": self.run_id,
            "status": "passed",
            "reason": "comment_processed",
            "source_branch": source_branch,
            "command": command,
            "commit_sha": implementation.commit_sha,
            "changed_files": implementation.changed_files,
            "implementation": implementation.model_dump(mode="json"),
            "auto_approval": auto_approval_report,
            "gate": gate.model_dump(mode="json"),
        }

    def _validate_proposal(
        self,
        task: AgentTask,
        implementation: ImplementationReport,
        file_tool: FileTool,
    ) -> dict[str, Any]:
        proposed_diff = self._artifact_text("proposed.diff")
        risk = assess_risk(task, implementation.changed_files, proposed_diff)
        blockers: list[str] = []
        checks: dict[str, str] = {
            "risk": "failed" if risk.blocked else "passed",
            "docs_check": "skipped",
            "structure_evidence_check": "skipped",
        }
        if risk.blocked:
            blockers.extend(risk.reasons or ["risk assessment blocked proposed patch"])

        proposal_report = self._artifact_json("structured_proposal_report.json")
        if int(proposal_report.get("added_lines") or 0) > self.config.max_added_lines:
            checks["added_lines_under_limit"] = "failed"
            blockers.append("too many added lines")
        else:
            checks["added_lines_under_limit"] = "passed"
        if int(proposal_report.get("deleted_lines") or 0) > self.config.max_deleted_lines:
            checks["deleted_lines_under_limit"] = "failed"
            blockers.append("too many deleted lines")
        else:
            checks["deleted_lines_under_limit"] = "passed"
        if len(implementation.changed_files) > self.config.max_changed_files:
            checks["changed_files_under_limit"] = "failed"
            blockers.append("too many changed files")
        else:
            checks["changed_files_under_limit"] = "passed"
        touched_protected = proposal_report.get("touched_protected_paths")
        if isinstance(touched_protected, list) and touched_protected:
            checks["no_protected_paths"] = "failed"
            blockers.append("protected paths touched: " + ", ".join(str(path) for path in touched_protected))
        else:
            checks["no_protected_paths"] = "passed"
        if proposal_report.get("sensitive_content_detected") is True:
            checks["sensitive_content_absent"] = "failed"
            blockers.append("secrets touched")
        else:
            checks["sensitive_content_absent"] = "passed"

        docs_check = None
        if is_readme_only(implementation.changed_files):
            proposed_contents = self._proposal_content_overrides(file_tool)
            docs_check = DocsCheckAgent(file_tool, self.artifacts, content_overrides=proposed_contents).run(implementation.changed_files)
            self.artifacts.write_json("docs_check_report", docs_check)
            checks["docs_check"] = docs_check.docs_check
            checks["structure_evidence_check"] = docs_check.structure_evidence_check
            if docs_check.docs_check == "failed":
                blockers.append("docs check failed")
            if docs_check.structure_evidence_check == "failed":
                blockers.append("project structure evidence failed")
            for finding in docs_check.findings:
                if finding.blocked and finding.title not in blockers:
                    blockers.append(finding.title)

        validation = {
            "status": "failed" if blockers else "passed",
            "blockers": blockers,
            "checks": checks,
            "risk_score": risk.score,
            "risk_reasons": risk.reasons,
            "docs_check": docs_check.model_dump(mode="json") if docs_check is not None else None,
        }
        self.artifacts.write_json("proposal_validation_report", validation)
        self._update_structured_proposal_report(implementation, validation)
        return validation

    def _proposal_content_overrides(self, file_tool: FileTool) -> dict[str, str]:
        path = self.artifacts.artifacts_dir / "structured_proposal.json"
        if not path.exists():
            return {}
        try:
            proposal = StructuredEditProposal.model_validate_json(path.read_text(encoding="utf-8"))
            _, _, proposed_files = file_tool.preview_structured_edits(proposal)
            return proposed_files
        except Exception:
            return {}

    def _ensure_proposal_worktree_clean(self, git_tool: GitTool, changed_files: list[str]) -> dict[str, Any]:
        dirty_before = git_tool.status_porcelain()
        if not dirty_before:
            return {"clean": True, "dirty_before": "", "dirty_after": "", "cleanup_attempted": False}
        cleanup = git_tool.restore_paths(changed_files)
        dirty_after = git_tool.status_porcelain()
        return {
            "clean": not dirty_after,
            "dirty_before": dirty_before,
            "dirty_after": dirty_after,
            "cleanup_attempted": True,
            "cleanup_command": cleanup.command,
            "cleanup_exit_code": cleanup.exit_code,
            "cleanup_stderr": cleanup.stderr,
        }

    def _artifact_text(self, name: str) -> str:
        path = self.artifacts.artifacts_dir / name
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _artifact_json(self, name: str) -> dict[str, Any]:
        path = self.artifacts.artifacts_dir / name
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _update_structured_proposal_report(
        self,
        implementation: ImplementationReport,
        proposal_validation: dict[str, Any],
    ) -> None:
        payload = self._artifact_json("structured_proposal_report.json")
        payload.update(
            {
                "run_id": self.run_id,
                "source_branch": implementation.branch,
                "status": "proposal_validation_" + str(proposal_validation.get("status") or "unknown"),
                "proposal_validation": proposal_validation,
            }
        )
        self.artifacts.write_json("structured_proposal_report", payload)

    def _build_revision_context(
        self,
        git_tool: GitTool,
        file_tool: FileTool,
        *,
        mr_iid: int,
        source_branch: str,
        base_ref: str,
        command: str,
        feedback: str,
        changed_files: list[str],
    ) -> dict[str, Any]:
        tracked_files = self._revision_snapshot_paths(changed_files, feedback)
        base_snapshot = {
            "run_id": self.run_id,
            "ref": base_ref,
            "source_branch": source_branch,
            "files": [self._base_file_snapshot(git_tool, base_ref, path) for path in tracked_files],
        }
        mr_snapshot = {
            "run_id": self.run_id,
            "ref": source_branch,
            "source_branch": source_branch,
            "files": [self._mr_file_snapshot(file_tool, path) for path in tracked_files],
        }
        self.artifacts.write_json("base_file_snapshot", base_snapshot)
        self.artifacts.write_json("mr_file_snapshot", mr_snapshot)

        previous_commits = self._previous_agent_commits(git_tool, base_ref)
        previous_artifacts = self._previous_revision_artifacts()
        base_by_path = {str(item["path"]): item for item in base_snapshot["files"]}
        mr_by_path = {str(item["path"]): item for item in mr_snapshot["files"]}
        structured_diff_summary = [
            self._structured_revision_file_summary(
                path,
                base_by_path.get(path, {}),
                mr_by_path.get(path, {}),
                feedback=feedback,
            )
            for path in tracked_files
        ]
        context = {
            "run_id": self.run_id,
            "mr_iid": mr_iid,
            "source_branch": source_branch,
            "base_ref": base_ref,
            "command": command,
            "user_requested_change": feedback,
            "changed_files": changed_files,
            "snapshot_files": tracked_files,
            "base_file_snapshot_artifact": "base_file_snapshot.json",
            "mr_file_snapshot_artifact": "mr_file_snapshot.json",
            "structured_diff_summary": structured_diff_summary,
            "previous_agent_commits": previous_commits,
            "previous_artifacts": previous_artifacts,
        }
        self.artifacts.write_json("revision_context", context)
        return context

    def _revision_snapshot_paths(self, changed_files: list[str], feedback: str) -> list[str]:
        paths = _dedupe_paths(changed_files)
        if _feedback_requests_base_readme(feedback) and "README.md" not in paths:
            paths.insert(0, "README.md")
        return paths

    def _base_file_snapshot(self, git_tool: GitTool, base_ref: str, path: str) -> dict[str, Any]:
        try:
            content = git_tool.show_file(base_ref, path)
            return {"path": path, "exists": True, "content": content, "error": None}
        except Exception as exc:
            return {"path": path, "exists": False, "content": "", "error": str(exc)}

    def _mr_file_snapshot(self, file_tool: FileTool, path: str) -> dict[str, Any]:
        try:
            content = file_tool.read_file(path)
            return {"path": path, "exists": True, "content": content, "error": None}
        except Exception as exc:
            return {"path": path, "exists": False, "content": "", "error": str(exc)}

    def _structured_revision_file_summary(
        self,
        path: str,
        base_snapshot: dict[str, Any],
        mr_snapshot: dict[str, Any],
        *,
        feedback: str,
    ) -> dict[str, Any]:
        base_content = str(base_snapshot.get("content") or "")
        mr_content = str(mr_snapshot.get("content") or "")
        base_block = _revision_relevant_block(path, base_content, feedback)
        mr_block = _revision_relevant_block(path, mr_content, feedback)
        return {
            "path": path,
            "base_branch_block": base_block,
            "current_mr_block": mr_block,
            "user_requested_change": feedback,
            "intended_final_block": _intended_revision_block(path, base_block, mr_block, feedback),
            "base_exists": bool(base_snapshot.get("exists")),
            "mr_exists": bool(mr_snapshot.get("exists")),
            "base_error": base_snapshot.get("error"),
            "mr_error": mr_snapshot.get("error"),
        }

    def _previous_agent_commits(self, git_tool: GitTool, base_ref: str) -> list[dict[str, str]]:
        try:
            commits = git_tool.commit_log(base_ref, "HEAD", max_count=20)
        except Exception:
            return []
        previous: list[dict[str, str]] = []
        for commit in commits:
            subject = commit.get("subject", "")
            author = f"{commit.get('author_name', '')} {commit.get('author_email', '')}".lower()
            if subject.startswith("agent:") or "agentlab" in author:
                previous.append(commit)
        return previous

    def _previous_revision_artifacts(self) -> list[dict[str, Any]]:
        names = {
            "project_structure_evidence.json",
            "implementation_report.json",
            "structured_edit_apply_report.json",
            "structured_edit_error.json",
            "revision_context.json",
        }
        root = Path(self.config.workspace_root)
        current_artifacts = self.artifacts.artifacts_dir.resolve()
        candidates = sorted(root.glob("*/artifacts/*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        artifacts: list[dict[str, Any]] = []
        for path in candidates:
            if path.name not in names:
                continue
            try:
                if path.parent.resolve() == current_artifacts:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            artifacts.append(
                {
                    "run_id": path.parent.parent.name,
                    "name": path.name,
                    "path": str(path),
                    "payload": payload,
                }
            )
            if len(artifacts) >= 5:
                break
        return artifacts

    def _review_comment_task(
        self,
        *,
        mr_iid: int,
        source_branch: str,
        command: str,
        feedback: str,
        note_id: int | str,
        affected_files: list[str],
        revision_context: dict[str, Any] | None = None,
    ) -> AgentTask:
        task_type = _review_task_type(command, affected_files)
        safe_note_id = "".join(char if char.isalnum() else "-" for char in str(note_id)).strip("-") or "note"
        if command == "fix":
            title = f"Fix MR !{mr_iid} from review comment"
        elif command in {"propose", "dry-run"}:
            title = f"Propose MR !{mr_iid} revision from review comment"
        else:
            title = f"Revise MR !{mr_iid} from review comment"
        return AgentTask(
            id=f"mr-{mr_iid}-{command}-{safe_note_id}"[:80].strip("-"),
            title=title,
            task_type=task_type,
            risk_level=RiskLevel.LOW if task_type in {TaskType.DOCS, TaskType.TESTS} else RiskLevel.MEDIUM,
            risk_score=1 if task_type in {TaskType.DOCS, TaskType.TESTS} else 3,
            description=(
                f"Apply the authorized GitLab MR comment command `/agent {command}` to existing branch "
                f"`{source_branch}`.\n\nFeedback from the comment:\n{feedback or '<no extra feedback>'}"
            ),
            acceptance_criteria=[
                "Update the existing merge request branch only.",
                "Respect the configured allowed_paths and blocked_paths policy.",
                "Do not execute comment text as a shell command.",
                "Do not enable auto-merge or direct-main push.",
            ],
            affected_files=affected_files,
            forbidden_actions=[
                "Do not execute comment text as shell, bash, Python, or any other command.",
                "Do not change files outside affected_files.",
                "Do not create a new merge request.",
                "Do not enable direct-main pushes or auto-merge.",
            ],
            test_requirements=[] if task_type == TaskType.DOCS else list(self.config.required_test_commands),
            metadata={
                "source": "gitlab_mr_comment",
                "mr_iid": mr_iid,
                "note_id": str(note_id),
                "command": command,
                "source_branch": source_branch,
                "changed_files": affected_files,
                "revision_context": revision_context or {},
            },
        )


def _review_task_type(command: str, affected_files: list[str]) -> TaskType:
    normalized = [path.replace("\\", "/").lower() for path in affected_files]
    if normalized and all(path.startswith("tests/") or "/tests/" in path or path.endswith((".test.ts", "_test.py")) for path in normalized):
        return TaskType.TESTS
    if normalized and all(path.startswith("docs/") or path.endswith((".md", ".markdown")) or path.rsplit("/", 1)[-1].startswith("readme") for path in normalized):
        return TaskType.DOCS
    if command == "fix":
        return TaskType.BUGFIX
    return TaskType.DOCS if not normalized else TaskType.REFACTOR


def _auto_approval_report_from_approved_plan(plan: TaskPlan, selected_task_id: str | None) -> dict[str, Any]:
    return {
        "enabled": True,
        "policy_name": "auto_approval",
        "policy_version": "from_approved_plan",
        "evaluated_tasks": [
            {
                "task_id": task.id,
                "approved": task.approved,
                "reasons": ["loaded_from_approved_plan"],
                "details": {},
            }
            for task in plan.tasks
        ],
        "approved_tasks": [task.id for task in plan.tasks if task.approved],
        "rejected_tasks": [
            {
                "task_id": task.id,
                "reasons": ["not_approved_in_approved_plan"],
                "details": {},
            }
            for task in plan.tasks
            if not task.approved
        ],
        "selected_task_id": selected_task_id,
    }


README_BASE_CONTEXT_RE = re.compile(
    r"\b(main|restore|detailtiefe|actual files|real structure|origin/main|base branch|wiederherstell|tatsaechlich|tatsächlich)\b",
    re.IGNORECASE,
)
README_STRUCTURE_HEADING_RE = re.compile(
    r"^(#{1,6})\s*((?:project|repository|file|directory)\s+structure)\s*:?\s*$",
    re.IGNORECASE,
)


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        normalized = str(path).replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _proposal_artifacts_from_report(report: ImplementationReport) -> list[str]:
    wanted = ["structured_proposal.json", "proposed.diff", "structured_proposal_report.json"]
    present = {str(name) for name in report.patch_artifacts}
    return [name for name in wanted if name in present]


def _feedback_requests_base_readme(feedback: str) -> bool:
    return bool(README_BASE_CONTEXT_RE.search(feedback or ""))


def _revision_relevant_block(path: str, content: str, feedback: str) -> str:
    if not content:
        return ""
    normalized = path.replace("\\", "/").lower()
    if normalized.rsplit("/", 1)[-1].startswith("readme") and _feedback_requests_base_readme(feedback):
        structure_block = _readme_structure_block(content)
        if structure_block:
            return structure_block
    return _excerpt_text(content)


def _readme_structure_block(content: str) -> str:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = README_STRUCTURE_HEADING_RE.match(line.strip())
        if not match:
            continue
        level = len(match.group(1))
        end = len(lines)
        for candidate in range(index + 1, len(lines)):
            heading = re.match(r"^(#{1,6})\s+", lines[candidate].strip())
            if heading and len(heading.group(1)) <= level:
                end = candidate
                break
        return "".join(lines[index:end])
    return ""


def _intended_revision_block(path: str, base_block: str, mr_block: str, feedback: str) -> str:
    normalized = path.replace("\\", "/").lower()
    if normalized.rsplit("/", 1)[-1].startswith("readme") and _feedback_requests_base_readme(feedback) and base_block:
        return base_block
    if mr_block:
        return (
            "Apply the user-requested change to the current MR block while preserving existing detail; "
            "do not compact or simplify unless the comment explicitly asks for a summary."
        )
    return "No existing MR block was available; derive the final content only from explicit repository evidence."


def _excerpt_text(content: str, limit: int = 20_000) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + "\n<excerpt truncated>"
