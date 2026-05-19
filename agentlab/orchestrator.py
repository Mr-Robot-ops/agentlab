from __future__ import annotations

import uuid
from pathlib import Path

from agentlab.artifacts import ArtifactStore
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
    GateDecision,
    ImplementationReport,
    ProvenanceStatement,
    RepoIndex,
    ReportStatus,
    StewardReport,
    SupplyChainReport,
    TaskPlan,
)
from agentlab.preflight import PreflightChecker
from agentlab.policies.policy_engine import PolicyEngine
from agentlab.policies.risk import assess_risk
from agentlab.provenance import ProvenanceBuilder
from agentlab.repo_indexer import RepoIndexer
from agentlab.repo_policy import apply_repo_policy, load_repo_policy
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
        with self.audit.span(agent="implementer", action="implement", input_payload=task.model_dump(mode="json")):
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
            ).implement(task)
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

    def full_flow(self) -> dict[str, object]:
        self.audit.emit(agent="orchestrator", action="full_flow", status="started")
        try:
            plan = self.plan()
            approved_tasks = [task for task in plan.tasks if task.approved]
            if not approved_tasks:
                result = {
                    "run_id": self.run_id,
                    "status": "blocked",
                    "reason": "no approved task available for implementation",
                    "plan": plan.model_dump(mode="json"),
                }
                self.audit.emit(
                    agent="orchestrator",
                    action="full_flow",
                    status="blocked",
                    metadata={"reason": result["reason"]},
                    output_payload=result,
                )
                return result
            task = approved_tasks[0]
            implementation = self.run_task(task)
            if implementation.status != ReportStatus.PASSED:
                result = {"run_id": self.run_id, "status": "failed", "implementation": implementation.model_dump(mode="json")}
                self.audit.emit(
                    agent="orchestrator",
                    action="full_flow",
                    status="failed",
                    metadata={"reason": "implementation failed"},
                    output_payload=result,
                )
                return result
            mr_result: dict[str, object]
            if self.config.push_agent_branches_enabled and not self.dry_run:
                try:
                    with self.audit.span(agent="mr_agent", action="create_or_update_mr"):
                        mr = MergeRequestAgent(self.config, GitLabTool(self.config)).create_or_update(
                            task=task,
                            implementation=implementation,
                        )
                    mr_result = {"status": "created", "mr": mr.model_dump(mode="json")}
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
            decision = self.review_and_gate(task)
            provenance = self.provenance() if self.config.provenance_enabled else None
            result = {
                "run_id": self.run_id,
                "status": "passed" if decision.allowed else "blocked",
                "implementation": implementation.model_dump(mode="json"),
                "merge_request": mr_result,
                "gate": decision.model_dump(mode="json"),
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
                rollback_plan=rollback_plan,
                direct_main_push=direct_main_push,
            )
        self.artifacts.write_json("risk_assessment", risk)
        self.artifacts.write_json("diff_stats", diff_stats)
        self.artifacts.write_json("gate_decision", decision)
        return decision

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
