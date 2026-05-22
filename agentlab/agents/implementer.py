from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.branching import agent_branch_name
from agentlab.config import AppConfig
from agentlab.models import AgentTask, DiffStats, ImplementationReport, PatchProposal, ReportStatus, StructuredEditProposal, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import (
    FileTool,
    PatchApplyError,
    StructuredEditError,
    UnifiedDiffValidationError,
    structured_anchor_diagnostics,
)
from agentlab.tools.git_tool import GitTool
from agentlab.tools.ollama_client import OllamaClient, OllamaSchemaValidationError

from .base import compact_text, load_prompt


PROJECT_STRUCTURE_HEADING_RE = re.compile(
    r"^(#{1,6})\s*((?:project|repository|file|directory)\s+structure)\s*:?\s*$",
    re.IGNORECASE,
)
COMPACT_SUMMARY_RE = re.compile(r"\b(compact|concise|summary|summar(?:y|ize|ise|ized|ised)|high-level|abridged)\b", re.IGNORECASE)
ACTUAL_STRUCTURE_RE = re.compile(
    r"\b(match actual files|actual files|real structure|real repository|complete file tree|vollstaendige|vollständige|tatsaechlich|tatsächlich)\b",
    re.IGNORECASE,
)
PROJECT_STRUCTURE_ROOTS = (".github", "rust-backend", "web")
PROJECT_STRUCTURE_MAX_DEPTH = 4


class StructuredEditApplyError(ToolError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StructuredEditSchemaError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = "schema_validation_failed"


class GitPushError(ToolError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ProjectStructureValidationError(ToolError):
    def __init__(self, evidence: dict[str, object], artifact_names: list[str]) -> None:
        removed = evidence.get("removed_existing_entries") or []
        if removed:
            message = "README project structure edit removes existing files: " + ", ".join(str(item) for item in removed)
        else:
            message = "README project structure edit failed evidence validation"
        super().__init__(message)
        self.reason = "project_structure_validation_failed"
        self.evidence = evidence
        self.artifact_names = artifact_names


class ImplementationAgent:
    name = "implementer"

    def __init__(
        self,
        config: AppConfig,
        git_tool: GitTool,
        file_tool: FileTool,
        ollama: OllamaClient | None = None,
        *,
        dry_run: bool = False,
        repo_context: dict[str, Any] | None = None,
        artifacts: ArtifactStore | None = None,
        run_id: str | None = None,
    ) -> None:
        self.config = config
        self.git_tool = git_tool
        self.file_tool = file_tool
        self.ollama = ollama
        self.dry_run = dry_run
        self.repo_context = repo_context or {}
        self.artifacts = artifacts
        self.run_id = run_id or (artifacts.run_id if artifacts is not None else uuid.uuid4().hex)

    def implement(self, task: AgentTask) -> ImplementationReport:
        branch = agent_branch_name(task.id, self.run_id)
        errors: list[str] = []
        patch_artifacts: list[str] = []
        retry_attempted = False
        retry_succeeded = False
        commit_created = False
        branch_pushed = False
        commit_sha: str | None = None
        pushed = False
        first_patch_failure: PatchApplyError | UnifiedDiffValidationError | None = None
        implementation_mode = "structured_edit" if self._is_docs_task(task) else "patch"
        fallback_attempted = False
        fallback_succeeded = False
        fallback_reason: str | None = None
        if not task.approved:
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["task is not approved for implementation"],
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )

        try:
            checkout = self.git_tool.create_branch(branch, self.config.default_branch)
            if not checkout.ok:
                raise ToolError(checkout.stderr or "could not create agent branch")
            if implementation_mode == "structured_edit":
                try:
                    structured, raw_response = self._structured_proposal(task)
                except OllamaSchemaValidationError as exc:
                    patch_artifacts.extend(self._persist_structured_schema_error(exc))
                    raise StructuredEditSchemaError(self._structured_schema_message(exc.raw_response)) from exc
                patch_artifacts.extend(self._persist_structured_inputs(raw_response, structured))
                try:
                    diff_stats, evidence_artifacts = self._validate_and_apply_structured_with_evidence(
                        task,
                        structured,
                        source_branch=branch,
                    )
                    patch_artifacts.extend(evidence_artifacts)
                except Exception as structured_exc:
                    patch_artifacts.extend(self._project_structure_artifacts_from_error(structured_exc))
                    patch_artifacts.extend(self._persist_structured_anchor_error(structured, structured_exc))
                    patch_artifacts.extend(self._persist_structured_error(structured_exc))
                    if self._is_structured_repair_candidate(structured_exc):
                        retry_attempted = True
                        repair_proposal = structured
                        try:
                            repaired, repaired_raw = self._repair_structured_proposal(task, structured, structured_exc)
                            repair_proposal = repaired
                            patch_artifacts.extend(self._persist_structured_inputs(repaired_raw, repaired, repair=True))
                            diff_stats, evidence_artifacts = self._validate_and_apply_structured_with_evidence(
                                task,
                                repaired,
                                source_branch=branch,
                            )
                            patch_artifacts.extend(evidence_artifacts)
                        except Exception as repair_exc:
                            patch_artifacts.extend(self._project_structure_artifacts_from_error(repair_exc))
                            patch_artifacts.extend(self._persist_structured_anchor_error(repair_proposal, repair_exc, repair=True))
                            patch_artifacts.extend(self._persist_structured_error(repair_exc, repair=True))
                            raise StructuredEditApplyError(
                                self._structured_failure_reason(repair_exc),
                                self._structured_retry_failure_message(structured_exc, repair_exc),
                            ) from repair_exc
                        patch_artifacts.extend(self._persist_structured_apply_report(repaired, diff_stats, repair=True))
                        structured = repaired
                        retry_succeeded = True
                    else:
                        raise StructuredEditApplyError(
                            self._structured_failure_reason(structured_exc),
                            self._structured_apply_failure_message(structured_exc),
                        ) from structured_exc
                patch_artifacts.extend(self._persist_structured_apply_report(structured, diff_stats))
                summary = structured.summary
                expected_tests = structured.expected_tests
                risk_input = structured.model_dump_json()
            else:
                proposal, raw_response = self._proposal(task)
                patch_artifacts.extend(self._persist_patch_inputs(raw_response, proposal))
                try:
                    diff_stats = self._validate_and_apply(task, proposal)
                except (PatchApplyError, UnifiedDiffValidationError) as exc:
                    first_patch_failure = exc
                    patch_artifacts.extend(self._persist_patch_failure(exc))
                    if self._is_docs_task(task) and self._is_patch_fallback_candidate(exc):
                        fallback_attempted = True
                        fallback_reason = self._patch_failure_reason(exc)
                        try:
                            structured, structured_raw = self._structured_proposal(task, fallback_reason=fallback_reason)
                        except OllamaSchemaValidationError as schema_exc:
                            patch_artifacts.extend(self._persist_structured_schema_error(schema_exc, fallback_reason=fallback_reason))
                            raise StructuredEditSchemaError(self._structured_schema_message(schema_exc.raw_response)) from schema_exc
                        patch_artifacts.extend(self._persist_structured_inputs(structured_raw, structured))
                        try:
                            diff_stats, evidence_artifacts = self._validate_and_apply_structured_with_evidence(
                                task,
                                structured,
                                source_branch=branch,
                            )
                            patch_artifacts.extend(evidence_artifacts)
                        except Exception as structured_exc:
                            patch_artifacts.extend(self._project_structure_artifacts_from_error(structured_exc))
                            patch_artifacts.extend(self._persist_structured_anchor_error(structured, structured_exc))
                            patch_artifacts.extend(self._persist_structured_error(structured_exc, fallback_reason=fallback_reason))
                            raise StructuredEditApplyError(
                                self._structured_failure_reason(structured_exc),
                                self._structured_apply_failure_message(structured_exc),
                            ) from structured_exc
                        patch_artifacts.extend(
                            self._persist_structured_apply_report(
                                structured,
                                diff_stats,
                                fallback_reason=fallback_reason,
                                fallback_from="PatchProposal",
                                fallback_to="StructuredEditProposal",
                            )
                        )
                        implementation_mode = "structured_edit"
                        fallback_succeeded = True
                        summary = structured.summary
                        expected_tests = structured.expected_tests
                        risk_input = structured.model_dump_json()
                    else:
                        if isinstance(exc, PatchApplyError) and not self._is_corrupt_patch(exc.stderr):
                            raise
                        retry_attempted = True
                        repaired, repaired_raw = self._repair_patch(
                            task,
                            proposal.patch,
                            exc.stderr if isinstance(exc, PatchApplyError) else "",
                            validation_error=exc if isinstance(exc, UnifiedDiffValidationError) else None,
                        )
                        patch_artifacts.extend(self._persist_patch_inputs(repaired_raw, repaired, prefix="repair_"))
                        try:
                            diff_stats = self._validate_and_apply(task, repaired)
                            proposal = repaired
                            retry_succeeded = True
                        except (PatchApplyError, UnifiedDiffValidationError) as retry_exc:
                            patch_artifacts.extend(self._persist_patch_failure(retry_exc, prefix="repair_"))
                            raise retry_exc
                if implementation_mode == "patch":
                    summary = proposal.summary
                    expected_tests = proposal.expected_tests
                    risk_input = proposal.patch
            risk = assess_risk(task, diff_stats.changed_files, risk_input)
            if risk.blocked:
                raise ToolError("risk assessment blocked patch: " + ", ".join(risk.reasons))
            commit_sha = self.git_tool.commit(f"agent: {task.title}")
            commit_created = commit_sha is not None
            if self.config.push_agent_branches_enabled and not self.dry_run:
                push = self.git_tool.push(branch)
                if not push.ok:
                    raise GitPushError(self._push_failure_reason(push.stderr), self._short_error(push.stderr or "git push failed"))
                pushed = True
                branch_pushed = True
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.PASSED,
                applied=not self.dry_run,
                pushed=pushed,
                commit_sha=commit_sha,
                patch_summary=summary,
                changed_files=diff_stats.changed_files,
                risk_score=risk.score,
                tests_recommended=expected_tests,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except StructuredEditApplyError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_apply",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode="structured_edit",
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except StructuredEditSchemaError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_schema_validation",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode="structured_edit",
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except PatchApplyError as exc:
            if first_patch_failure is not None and first_patch_failure is not exc:
                errors.append(str(first_patch_failure))
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="patch_apply",
                failure_reason="corrupt_patch" if self._is_corrupt_patch(exc.stderr) else "patch_apply_failed",
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except UnifiedDiffValidationError as exc:
            if first_patch_failure is not None and first_patch_failure is not exc:
                errors.append(str(first_patch_failure))
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="patch_validation",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except GitPushError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                applied=not self.dry_run,
                pushed=False,
                commit_sha=commit_sha,
                errors=errors,
                failure_stage="git_push",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )
        except Exception as exc:
            errors.append(str(exc))
            failure_stage = None
            failure_reason = None
            if self._is_git_author_identity_missing(exc):
                failure_stage = "git_commit"
                failure_reason = "git_author_identity_missing"
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                pushed=False,
                commit_sha=commit_sha,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
                fallback_attempted=fallback_attempted,
                fallback_succeeded=fallback_succeeded,
                fallback_reason=fallback_reason,
            )

    def revise_on_branch(self, task: AgentTask, branch: str) -> ImplementationReport:
        errors: list[str] = []
        patch_artifacts: list[str] = []
        commit_sha: str | None = None
        commit_created = False
        branch_pushed = False
        implementation_mode = "structured_edit" if self._is_docs_task(task) else "patch"

        if not branch.startswith("agent/"):
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["revision branch must be an agent/* branch"],
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        if not task.approved:
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["task is not approved for implementation"],
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )

        try:
            checkout = self.git_tool.checkout(branch)
            if not checkout.ok:
                raise ToolError(checkout.stderr or f"could not checkout revision branch {branch}")

            if implementation_mode == "structured_edit":
                try:
                    structured, raw_response = self._structured_proposal(task)
                except OllamaSchemaValidationError as exc:
                    patch_artifacts.extend(self._persist_structured_schema_error(exc))
                    raise StructuredEditSchemaError(self._structured_schema_message(exc.raw_response)) from exc
                patch_artifacts.extend(self._persist_structured_inputs(raw_response, structured))
                try:
                    diff_stats, evidence_artifacts = self._validate_and_apply_structured_with_evidence(
                        task,
                        structured,
                        source_branch=branch,
                    )
                    patch_artifacts.extend(evidence_artifacts)
                except Exception as exc:
                    patch_artifacts.extend(self._project_structure_artifacts_from_error(exc))
                    patch_artifacts.extend(self._persist_structured_anchor_error(structured, exc))
                    patch_artifacts.extend(self._persist_structured_error(exc))
                    raise StructuredEditApplyError(self._structured_failure_reason(exc), self._structured_apply_failure_message(exc)) from exc
                patch_artifacts.extend(self._persist_structured_apply_report(structured, diff_stats))
                summary = structured.summary
                expected_tests = structured.expected_tests
                risk_input = structured.model_dump_json()
            else:
                proposal, raw_response = self._proposal(task)
                patch_artifacts.extend(self._persist_patch_inputs(raw_response, proposal))
                try:
                    diff_stats = self._validate_and_apply(task, proposal)
                except (PatchApplyError, UnifiedDiffValidationError) as exc:
                    patch_artifacts.extend(self._persist_patch_failure(exc))
                    raise
                summary = proposal.summary
                expected_tests = proposal.expected_tests
                risk_input = proposal.patch

            risk = assess_risk(task, diff_stats.changed_files, risk_input)
            if risk.blocked:
                raise ToolError("risk assessment blocked patch: " + ", ".join(risk.reasons))
            commit_sha = self.git_tool.commit(f"agent: revise MR feedback for {task.title}")
            commit_created = commit_sha is not None
            pushed = False
            if self.config.push_agent_branches_enabled and not self.dry_run and commit_created:
                push = self.git_tool.push(branch)
                if not push.ok:
                    raise GitPushError(self._push_failure_reason(push.stderr), self._short_error(push.stderr or "git push failed"))
                pushed = True
                branch_pushed = True
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.PASSED,
                applied=not self.dry_run,
                pushed=pushed,
                commit_sha=commit_sha,
                patch_summary=summary,
                changed_files=diff_stats.changed_files,
                risk_score=risk.score,
                tests_recommended=expected_tests,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except StructuredEditApplyError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_apply",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode="structured_edit",
            )
        except StructuredEditSchemaError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_schema_validation",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode="structured_edit",
            )
        except PatchApplyError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="patch_apply",
                failure_reason="corrupt_patch" if self._is_corrupt_patch(exc.stderr) else "patch_apply_failed",
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except UnifiedDiffValidationError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="patch_validation",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except GitPushError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                applied=not self.dry_run,
                pushed=False,
                commit_sha=commit_sha,
                errors=errors,
                failure_stage="git_push",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except Exception as exc:
            errors.append(str(exc))
            failure_stage = None
            failure_reason = None
            if self._is_git_author_identity_missing(exc):
                failure_stage = "git_commit"
                failure_reason = "git_author_identity_missing"
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                pushed=False,
                commit_sha=commit_sha,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )

    def propose_on_branch(self, task: AgentTask, branch: str) -> ImplementationReport:
        errors: list[str] = []
        patch_artifacts: list[str] = []
        implementation_mode = "structured_edit" if self._is_docs_task(task) else "patch"

        if not branch.startswith("agent/"):
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["revision branch must be an agent/* branch"],
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        if not task.approved:
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["task is not approved for proposal generation"],
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )

        try:
            checkout = self.git_tool.checkout(branch)
            if not checkout.ok:
                raise ToolError(checkout.stderr or f"could not checkout revision branch {branch}")

            if implementation_mode == "structured_edit":
                try:
                    structured, raw_response = self._structured_proposal(task)
                except OllamaSchemaValidationError as exc:
                    patch_artifacts.extend(self._persist_structured_schema_error(exc))
                    raise StructuredEditSchemaError(self._structured_schema_message(exc.raw_response)) from exc
                patch_artifacts.extend(self._persist_structured_inputs(raw_response, structured))
                try:
                    self._ensure_structured_task_scope(task, structured)
                    evidence_artifacts = self._validate_readme_project_structure_edit(
                        task,
                        structured,
                        source_branch=branch,
                        base_ref=self.config.default_branch,
                    )
                    patch_artifacts.extend(evidence_artifacts)
                    diff_stats, proposed_diff, _ = self.file_tool.preview_structured_edits(structured)
                except Exception as exc:
                    patch_artifacts.extend(self._project_structure_artifacts_from_error(exc))
                    patch_artifacts.extend(self._persist_structured_anchor_error(structured, exc))
                    patch_artifacts.extend(self._persist_structured_error(exc))
                    raise StructuredEditApplyError(self._structured_failure_reason(exc), self._structured_apply_failure_message(exc)) from exc
                summary = structured.summary
                expected_tests = structured.expected_tests
                risk_input = structured.model_dump_json()
                patch_artifacts.extend(self._persist_proposal_artifacts(structured, proposed_diff, diff_stats))
            else:
                proposal, raw_response = self._proposal(task)
                patch_artifacts.extend(self._persist_patch_inputs(raw_response, proposal))
                self._ensure_task_scope(task, proposal)
                diff_stats = self.file_tool.validate_patch(proposal)
                summary = proposal.summary
                expected_tests = proposal.expected_tests
                risk_input = proposal.patch
                patch_artifacts.extend(self._persist_proposal_artifacts(proposal, proposal.patch, diff_stats))

            risk = assess_risk(task, diff_stats.changed_files, risk_input)
            if risk.blocked:
                raise ToolError("risk assessment blocked proposed patch: " + ", ".join(risk.reasons))

            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.PASSED,
                applied=False,
                pushed=False,
                commit_sha=None,
                patch_summary=summary,
                changed_files=diff_stats.changed_files,
                risk_score=risk.score,
                tests_recommended=expected_tests,
                patch_artifacts=patch_artifacts,
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except StructuredEditApplyError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_apply",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode="structured_edit",
            )
        except StructuredEditSchemaError as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="structured_edit_schema_validation",
                failure_reason=exc.reason,
                patch_artifacts=patch_artifacts,
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode="structured_edit",
            )
        except (PatchApplyError, UnifiedDiffValidationError) as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="patch_validation",
                failure_reason=getattr(exc, "reason", "patch_validation_failed"),
                patch_artifacts=patch_artifacts,
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )
        except Exception as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
                failure_stage="proposal_generation",
                failure_reason="proposal_generation_failed",
                patch_artifacts=patch_artifacts,
                no_changes_committed=True,
                no_branch_pushed=True,
                implementation_mode=implementation_mode,  # type: ignore[arg-type]
            )

    def _proposal(self, task: AgentTask) -> tuple[PatchProposal, str]:
        if self.ollama is None:
            raise ToolError("OllamaClient is required for active implementation patches")
        snippets: dict[str, str] = {}
        for path in task.affected_files[:10]:
            try:
                snippets[path] = compact_text(self.file_tool.read_file(path), 8_000)
            except Exception as exc:
                snippets[path] = f"<unreadable: {exc}>"
        payload = {
            "task": task.model_dump(mode="json"),
            "repo_context": self.repo_context,
            "file_snippets": snippets,
            "rules": {
                "output": "Return exactly one unified diff in PatchProposal.patch.",
                "scope": "Only touch files needed for this task.",
                "forbidden_actions": task.forbidden_actions,
                "whole_repo_awareness": "Use repo_context to respect architecture, test strategy, ownership boundaries, and deployment signals.",
            },
        }
        proposal, raw_response = self._chat_patch_proposal(
            system_prompt=load_prompt("implementer.md"),
            user_prompt=json.dumps(payload, indent=2),
        )
        if proposal.task_id != task.id:
            raise ToolError("PatchProposal.task_id does not match task id")
        return proposal, raw_response

    def _structured_proposal(self, task: AgentTask, *, fallback_reason: str | None = None) -> tuple[StructuredEditProposal, str]:
        snippets = self._file_snippets(task)
        project_structure_context = self._project_structure_prompt_context(task)
        payload = {
            "task": task.model_dump(mode="json"),
            "repo_context": self.repo_context,
            "file_snippets": snippets,
            "fallback_reason": fallback_reason,
                "rules": {
                    "output": "Return only one StructuredEditProposal JSON object.",
                    "format": "Do not return a unified diff. Do not use Markdown fences. Do not include explanations outside JSON.",
                    "example": {
                        "task_id": task.id,
                        "summary": "Short description of the documentation change.",
                        "edits": [
                            {
                                "path": "README.md",
                                "operation": "replace_text",
                                "old_text": "exact old text",
                                "new_text": "replacement text",
                            }
                        ],
                        "expected_tests": [],
                        "risk_score": 1,
                        "rollback": "Revert the documentation edit.",
                        "metadata": {},
                    },
                    "field_names": "Use path and operation. Do not use file_path. Do not use tool.",
                    "scope": "Only edit files listed in task.affected_files.",
                "operations": {
                    "replace_text": "Use when replacing exactly one existing text block; old_text must match exactly once.",
                    "append_to_file": "Use only when appending to an existing file.",
                    "replace_file": "Use only when full target file content is safer than a small replacement.",
                    "insert_before": "Prefer this for adding a new Markdown section before an existing heading anchor.",
                    "insert_after": "Prefer this for adding a new Markdown section after an existing exact anchor line.",
                },
                "docs_guidance": [
                    "For Docs/Markdown tasks, use insert_before or insert_after for new sections.",
                    "Use replace_text only for small exact text copied from file_snippets.",
                    "Do not replace large multiline blocks when an insert is enough.",
                    "old_text must be copied exactly from file_snippets.",
                ],
                "insert_before_example": {
                    "path": "README.md",
                    "operation": "insert_before",
                    "anchor": "## Quick Start",
                    "content": "## Security & Privileged Mode\n...\n\n",
                },
                "insert_after_example": {
                    "path": "README.md",
                    "operation": "insert_after",
                    "anchor": "Open **http://localhost:8080** \u2014 default API key is `admin123`.",
                    "content": "\n\n### Deployment Assumptions\n...",
                },
                "forbidden_actions": task.forbidden_actions,
            },
        }
        if project_structure_context is not None:
            payload["project_structure_evidence"] = project_structure_context
            payload["rules"]["project_structure"] = [
                "When editing a Project Structure, Repository Structure, File Structure, or Directory Structure README section, use collected_files as the source of truth.",
                "Do not remove files present in collected_files unless the task explicitly asks for a compact summary.",
                "If the task explicitly asks for a compact summary, clearly state in the README block that it is a summary and not a complete file tree.",
            ]
        proposal, raw_response = self._chat_structured_proposal(
            system_prompt=load_prompt("implementer.md"),
            user_prompt=json.dumps(payload, indent=2),
        )
        if proposal.task_id != task.id:
            raise ToolError("StructuredEditProposal.task_id does not match task id")
        return proposal, raw_response

    def _repair_structured_proposal(
        self,
        task: AgentTask,
        proposal: StructuredEditProposal,
        exc: Exception,
    ) -> tuple[StructuredEditProposal, str]:
        anchor_diagnostic = self._structured_anchor_error_payload(proposal, exc)
        payload = {
            "task": task.model_dump(mode="json"),
            "original_structured_edit_proposal": proposal.model_dump(mode="json"),
            "structured_edit_error": self._structured_error_payload(exc),
            "structured_edit_anchor_diagnostic": anchor_diagnostic,
            "rules": {
                "output": "Return only one StructuredEditProposal JSON object.",
                "scope": "only repair anchors or old_text/new_text for the same affected files.",
                "same_affected_files": task.affected_files,
                "do_not_broaden_scope": True,
                "prefer_insert_operations": "For new Markdown sections, prefer insert_before or insert_after over replacing large blocks.",
                "readme_stale_section_retry": (
                    "If structured_edit_anchor_diagnostic.refreshed_section_context is present, repair the failing README "
                    "replace_text by using the exact current section as old_text and applying only the requested change."
                ),
                "no_markdown_fences": "Do not wrap JSON in Markdown fences.",
                "no_explanations": "Do not include explanations outside JSON.",
            },
        }
        proposal, raw_response = self._chat_structured_proposal(
            system_prompt=load_prompt("implementer.md"),
            user_prompt=json.dumps(payload, indent=2),
        )
        if proposal.task_id != task.id:
            raise ToolError("Repaired StructuredEditProposal.task_id does not match task id")
        return proposal, raw_response

    def _repair_patch(
        self,
        task: AgentTask,
        original_patch: str,
        stderr: str,
        *,
        validation_error: UnifiedDiffValidationError | None = None,
    ) -> tuple[PatchProposal, str]:
        validation_payload = validation_error.to_dict() if validation_error is not None else None
        payload = {
            "task": task.model_dump(mode="json"),
            "original_patch": original_patch,
            "git_apply_stderr": stderr,
            "patch_validation_error": validation_payload,
            "rules": {
                "repair_only": "Repair only unified diff syntax/format so git apply can parse it.",
                "hunk_prefix_rule": "Every line inside a unified diff hunk must start with space, +, -, or backslash.",
                "markdown_lines": "Markdown lines that should be added must start with +, for example +--- or +## Heading.",
                "scope": "Do not change task scope or intent.",
                "allowed_files": task.affected_files,
                "forbidden_actions": task.forbidden_actions,
                "output": "Return only one PatchProposal JSON object with the repaired patch.",
                "no_markdown_fences": "Do not wrap the diff in Markdown fences.",
                "no_explanations": "Do not include explanations outside JSON.",
            },
        }
        proposal, raw_response = self._chat_patch_proposal(
            system_prompt=load_prompt("implementer.md"),
            user_prompt=json.dumps(payload, indent=2),
        )
        if proposal.task_id != task.id:
            raise ToolError("Repaired PatchProposal.task_id does not match task id")
        return proposal, raw_response

    def _chat_patch_proposal(self, *, system_prompt: str, user_prompt: str) -> tuple[PatchProposal, str]:
        if self.ollama is None:
            raise ToolError("OllamaClient is required for active implementation patches")
        if hasattr(self.ollama, "chat_json_with_raw"):
            return self.ollama.chat_json_with_raw(
                model=self.config.agent_model("implementer"),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=PatchProposal,
            )
        proposal = self.ollama.chat_json(
            model=self.config.agent_model("implementer"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=PatchProposal,
        )
        return proposal, proposal.model_dump_json()

    def _chat_structured_proposal(self, *, system_prompt: str, user_prompt: str) -> tuple[StructuredEditProposal, str]:
        if self.ollama is None:
            raise ToolError("OllamaClient is required for active structured edits")
        if hasattr(self.ollama, "chat_json_with_raw"):
            return self.ollama.chat_json_with_raw(
                model=self.config.agent_model("implementer"),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=StructuredEditProposal,
            )
        proposal = self.ollama.chat_json(
            model=self.config.agent_model("implementer"),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=StructuredEditProposal,
        )
        return proposal, proposal.model_dump_json()

    def _persist_patch_inputs(self, raw_response: str, proposal: PatchProposal, *, prefix: str = "") -> list[str]:
        if self.artifacts is None:
            return []
        records = [
            self.artifacts.write_text(f"{prefix}implementer_raw_response.json", raw_response),
            self.artifacts.write_json(f"{prefix}patch_proposal", proposal),
            self.artifacts.write_text(f"{prefix}raw_patch.diff", proposal.patch),
        ]
        return [record.name for record in records]

    def _persist_patch_apply_error(self, exc: PatchApplyError, *, prefix: str = "") -> list[str]:
        if self.artifacts is None:
            return []
        records = [
            self.artifacts.write_text(f"{prefix}patch_apply_error.txt", str(exc)),
            self.artifacts.write_text(f"{prefix}patch_apply_stderr.txt", exc.stderr),
            self.artifacts.write_json(
                f"{prefix}patch_apply_command",
                {
                    "command": exc.command,
                    "stdin": "<patch via stdin>",
                    "check": exc.check,
                },
            ),
            self.artifacts.write_text(f"{prefix}patch_excerpt.txt", "\n".join(exc.patch.splitlines()[:80])),
        ]
        return [record.name for record in records]

    def _persist_patch_validation_error(self, exc: UnifiedDiffValidationError, *, prefix: str = "") -> list[str]:
        if self.artifacts is None:
            return []
        record = self.artifacts.write_json(f"{prefix}patch_validation_error", exc.to_dict())
        return [record.name]

    def _persist_patch_failure(self, exc: PatchApplyError | UnifiedDiffValidationError, *, prefix: str = "") -> list[str]:
        if isinstance(exc, PatchApplyError):
            return self._persist_patch_apply_error(exc, prefix=prefix)
        return self._persist_patch_validation_error(exc, prefix=prefix)

    def _persist_structured_inputs(self, raw_response: str, proposal: StructuredEditProposal, *, repair: bool = False) -> list[str]:
        if self.artifacts is None:
            return []
        raw_name = "structured_edit_repair_raw_response.json" if repair else "structured_edit_raw_response.json"
        proposal_name = "structured_edit_repair_proposal" if repair else "structured_edit_proposal"
        records = [
            self.artifacts.write_text(raw_name, raw_response),
            self.artifacts.write_json(proposal_name, proposal),
        ]
        return [record.name for record in records]

    def _persist_structured_apply_report(
        self,
        proposal: StructuredEditProposal,
        stats: DiffStats,
        *,
        fallback_reason: str | None = None,
        fallback_from: str | None = None,
        fallback_to: str | None = None,
        repair: bool = False,
    ) -> list[str]:
        if self.artifacts is None:
            return []
        record = self.artifacts.write_json(
            "structured_edit_repair_apply_report" if repair else "structured_edit_apply_report",
            {
                "status": "passed",
                "summary": proposal.summary,
                "changed_files": stats.changed_files,
                "added_lines": stats.added_lines,
                "deleted_lines": stats.deleted_lines,
                "fallback_reason": fallback_reason,
                "fallback_from": fallback_from,
                "fallback_to": fallback_to,
            },
        )
        return [record.name]

    def _persist_proposal_artifacts(
        self,
        proposal: PatchProposal | StructuredEditProposal,
        proposed_diff: str,
        stats: DiffStats,
    ) -> list[str]:
        if self.artifacts is None:
            return []
        records = []
        artifact_names = ["proposed.diff", "structured_proposal_report.json"]
        if isinstance(proposal, StructuredEditProposal):
            records.append(self.artifacts.write_json("structured_proposal", proposal))
            artifact_names.insert(0, "structured_proposal.json")
        records.append(self.artifacts.write_text("proposed.diff", proposed_diff))
        records.append(
            self.artifacts.write_json(
                "structured_proposal_report",
                {
                    "status": "generated",
                    "summary": proposal.summary,
                    "changed_files": stats.changed_files,
                    "added_lines": stats.added_lines,
                    "deleted_lines": stats.deleted_lines,
                    "secrets_touched": stats.secrets_touched,
                    "sensitive_content_detected": stats.secrets_touched,
                    "touched_protected_paths": stats.touched_protected_paths,
                    "proposal_artifacts": artifact_names,
                },
            )
        )
        return [record.name for record in records]

    def _persist_structured_error(
        self,
        exc: Exception,
        *,
        fallback_reason: str | None = None,
        repair: bool = False,
    ) -> list[str]:
        if self.artifacts is None:
            return []
        payload = self._structured_error_payload(exc)
        payload.update(
            {
                "status": "failed",
                "fallback_reason": fallback_reason,
                "no_changes_committed": True,
                "no_branch_pushed": True,
            }
        )
        record = self.artifacts.write_json(
            "structured_edit_repair_error" if repair else "structured_edit_error",
            payload,
        )
        return [record.name]

    def _persist_structured_anchor_error(
        self,
        proposal: StructuredEditProposal,
        exc: Exception,
        *,
        repair: bool = False,
    ) -> list[str]:
        if self.artifacts is None:
            return []
        payload = self._structured_anchor_error_payload(proposal, exc)
        if payload is None:
            return []
        payload.update(
            {
                "status": "failed",
                "diagnostic_only": True,
                "repo_write_performed": False,
                "no_changes_committed": True,
                "no_branch_pushed": True,
            }
        )
        record = self.artifacts.write_json(
            "structured_edit_repair_anchor_error" if repair else "structured_edit_anchor_error",
            payload,
        )
        return [record.name]

    def _structured_anchor_error_payload(
        self,
        proposal: StructuredEditProposal,
        exc: Exception,
    ) -> dict[str, object] | None:
        if not isinstance(exc, StructuredEditError) or exc.reason != "old_text_not_found":
            return None
        if exc.edit_index < 0 or exc.edit_index >= len(proposal.edits):
            return None

        edit = proposal.edits[exc.edit_index]
        old_text = edit.old_text or ""
        read_error: str | None = None
        current_text = ""
        try:
            current_text = self.file_tool.read_file(edit.path)
        except Exception as read_exc:
            read_error = str(read_exc)

        payload = self._structured_error_payload(exc)
        if current_text:
            payload.update(structured_anchor_diagnostics(path=edit.path, old_text=old_text, current_text=current_text))
        else:
            payload.update(
                {
                    "file_path": edit.path,
                    "old_text_preview": old_text[:500],
                    "old_text_repr_preview": ascii(old_text[:500]),
                    "old_text_hash": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
                    "nearby_candidate_matches": [],
                    "current_relevant_headings": [],
                }
            )
        payload.update(
            {
                "read_error": read_error,
                "retry_guidance": (
                    "Refresh the README replace_text old_text from the current target section, "
                    "or prefer insert_before/insert_after with a unique heading anchor."
                ),
            }
        )
        if current_text and self._is_readme_path(edit.path):
            payload["refreshed_section_context"] = self._readme_refreshed_section_context(current_text, old_text)
        return payload

    def _persist_structured_schema_error(
        self,
        exc: OllamaSchemaValidationError,
        *,
        fallback_reason: str | None = None,
    ) -> list[str]:
        if self.artifacts is None:
            return []
        raw_record = self.artifacts.write_text("structured_edit_raw_response.json", exc.raw_response)
        error_record = self.artifacts.write_json(
            "structured_edit_schema_error",
            {
                "validation_error_summary": exc.validation_error,
                "normalized_attempt": self._structured_normalized_attempt(exc.raw_response),
                "original_response": exc.raw_response,
                "expected_schema_hint": {
                    "edit_path_field": "path",
                    "edit_operation_field": "operation",
                    "allowed_operations": ["replace_file", "append_to_file", "replace_text"],
                    "do_not_use": ["file_path", "tool"],
                },
                "fallback_reason": fallback_reason,
            },
        )
        return [raw_record.name, error_record.name]

    def _validate_and_apply(self, task: AgentTask, proposal: PatchProposal) -> Any:
        self._ensure_task_scope(task, proposal)
        return self.file_tool.apply_patch(proposal)

    def _validate_and_apply_structured_with_evidence(
        self,
        task: AgentTask,
        proposal: StructuredEditProposal,
        *,
        source_branch: str,
    ) -> tuple[DiffStats, list[str]]:
        self._ensure_structured_task_scope(task, proposal)
        evidence_artifacts = self._validate_readme_project_structure_edit(
            task,
            proposal,
            source_branch=source_branch,
            base_ref=self.config.default_branch,
        )
        return self.file_tool.apply_structured_edits(proposal), evidence_artifacts

    def _validate_and_apply_structured(self, task: AgentTask, proposal: StructuredEditProposal) -> DiffStats:
        self._ensure_structured_task_scope(task, proposal)
        return self.file_tool.apply_structured_edits(proposal)

    def _validate_readme_project_structure_edit(
        self,
        task: AgentTask,
        proposal: StructuredEditProposal,
        *,
        source_branch: str,
        base_ref: str,
    ) -> list[str]:
        for path, old_content, proposed_content in self._preview_readme_updates(proposal):
            old_block, proposed_block = self._changed_project_structure_block(old_content, proposed_content)
            if old_block is None and proposed_block is None:
                continue

            evidence = self._project_structure_evidence(
                task,
                source_branch=source_branch,
                base_ref=base_ref,
                old_readme_block=old_block or "",
                proposed_readme_block=proposed_block or "",
            )
            artifact_names = self._persist_project_structure_evidence(evidence)
            if evidence["validation_status"] == "blocked":
                raise ProjectStructureValidationError(evidence, artifact_names)
            return artifact_names
        return []

    def _project_structure_prompt_context(self, task: AgentTask) -> dict[str, object] | None:
        readme_paths = [path for path in task.affected_files if self._is_readme_path(path)]
        if not readme_paths:
            return None

        readme_blocks: dict[str, str] = {}
        for path in readme_paths[:3]:
            try:
                blocks = self._readme_project_structure_blocks(self.file_tool.read_file(path))
            except Exception:
                blocks = []
            if blocks:
                readme_blocks[path] = blocks[0]["text"]

        if not readme_blocks and not self._task_mentions_project_structure(task):
            return None

        collected_files, ignored_files = self._collect_project_structure_files()
        return {
            "collection_command_equivalent": "find .github rust-backend web -maxdepth 4 -type f | sort",
            "collected_files": collected_files,
            "ignored_files": ignored_files,
            "old_readme_blocks": readme_blocks,
            "explicit_compact_summary_requested": self._task_allows_compact_structure_summary(task),
            "match_actual_files_requested": bool(ACTUAL_STRUCTURE_RE.search(self._task_text(task))),
        }

    def _project_structure_evidence(
        self,
        task: AgentTask,
        *,
        source_branch: str,
        base_ref: str,
        old_readme_block: str,
        proposed_readme_block: str,
    ) -> dict[str, object]:
        collected_files, ignored_files = self._collect_project_structure_files()
        old_entries = self._readme_tree_file_entries(old_readme_block)
        proposed_entries = self._readme_tree_file_entries(proposed_readme_block)
        collected = set(collected_files)
        removed_existing_entries = sorted(entry for entry in old_entries - proposed_entries if entry in collected)
        added_entries = sorted(proposed_entries - old_entries)
        compact_requested = self._task_allows_compact_structure_summary(task)
        validation_status = "passed"
        if removed_existing_entries and not compact_requested:
            validation_status = "blocked"
        elif removed_existing_entries and not self._block_declares_compact_summary(proposed_readme_block):
            validation_status = "blocked"

        return {
            "run_id": self.run_id,
            "source_branch": source_branch,
            "base_ref": base_ref,
            "target_repo_path": str(self.file_tool.repo_path),
            "collected_files": collected_files,
            "ignored_files": ignored_files,
            "old_readme_block": old_readme_block,
            "proposed_readme_block": proposed_readme_block,
            "removed_existing_entries": removed_existing_entries,
            "added_entries": added_entries,
            "validation_status": validation_status,
        }

    def _collect_project_structure_files(self) -> tuple[list[str], list[str]]:
        collected: list[str] = []
        ignored: list[str] = []
        for root in PROJECT_STRUCTURE_ROOTS:
            root_path = self.file_tool.repo_path / root
            if not root_path.exists():
                continue
            for path in root_path.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.file_tool.repo_path).as_posix()
                if len(relative.split("/")) - 1 > PROJECT_STRUCTURE_MAX_DEPTH:
                    continue
                if self._is_ignored_project_structure_file(relative):
                    ignored.append(relative)
                else:
                    collected.append(relative)
        return sorted(set(collected)), sorted(set(ignored))

    @staticmethod
    def _is_ignored_project_structure_file(path: str) -> bool:
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        return (
            normalized.startswith("web/dist/")
            or ".git" in parts
            or "node_modules" in parts
            or "target" in parts
        )

    def _persist_project_structure_evidence(self, evidence: dict[str, object]) -> list[str]:
        if self.artifacts is None:
            return []
        return [self.artifacts.write_json("project_structure_evidence", evidence).name]

    @staticmethod
    def _project_structure_artifacts_from_error(exc: Exception) -> list[str]:
        if isinstance(exc, ProjectStructureValidationError):
            return exc.artifact_names
        return []

    def _preview_readme_updates(self, proposal: StructuredEditProposal) -> list[tuple[str, str, str]]:
        working: dict[str, str] = {}
        original: dict[str, str] = {}
        for edit in proposal.edits:
            if not self._is_readme_path(edit.path):
                continue
            if edit.path not in working:
                try:
                    original[edit.path] = self.file_tool.read_file(edit.path)
                except Exception:
                    original[edit.path] = ""
                working[edit.path] = original[edit.path]
            old_content = working[edit.path]
            try:
                if edit.operation == "replace_file":
                    new_content = edit.content or ""
                elif edit.operation == "append_to_file":
                    new_content = old_content + (edit.content or "")
                elif edit.operation == "replace_text":
                    old_text = edit.old_text or ""
                    if old_content.count(old_text) != 1:
                        return []
                    new_content = old_content.replace(old_text, edit.new_text or "", 1)
                elif edit.operation in {"insert_before", "insert_after"}:
                    anchor = edit.anchor or ""
                    if old_content.count(anchor) != 1:
                        return []
                    insert_at = old_content.index(anchor)
                    if edit.operation == "insert_after":
                        insert_at += len(anchor)
                    new_content = old_content[:insert_at] + (edit.content or "") + old_content[insert_at:]
                else:  # pragma: no cover - pydantic validates operation
                    return []
            except Exception:
                return []
            working[edit.path] = new_content
        return [(path, original[path], proposed) for path, proposed in working.items() if original[path] != proposed]

    @staticmethod
    def _changed_project_structure_block(old_content: str, proposed_content: str) -> tuple[str | None, str | None]:
        old_blocks = ImplementationAgent._readme_project_structure_blocks(old_content)
        proposed_blocks = ImplementationAgent._readme_project_structure_blocks(proposed_content)
        keys = list(dict.fromkeys([block["key"] for block in old_blocks] + [block["key"] for block in proposed_blocks]))
        for key in keys:
            old_block = next((block["text"] for block in old_blocks if block["key"] == key), None)
            proposed_block = next((block["text"] for block in proposed_blocks if block["key"] == key), None)
            if old_block != proposed_block:
                return old_block, proposed_block
        return None, None

    @staticmethod
    def _readme_project_structure_blocks(text: str) -> list[dict[str, str]]:
        lines = text.splitlines(keepends=True)
        blocks: list[dict[str, str]] = []
        for index, line in enumerate(lines):
            match = PROJECT_STRUCTURE_HEADING_RE.match(line.strip())
            if not match:
                continue
            level = len(match.group(1))
            end = len(lines)
            for candidate in range(index + 1, len(lines)):
                heading = re.match(r"^(#{1,6})\s+", lines[candidate].strip())
                if heading and len(heading.group(1)) <= level:
                    end = candidate
                    break
            blocks.append({"key": match.group(2).lower(), "text": "".join(lines[index:end])})
        return blocks

    @staticmethod
    def _readme_tree_file_entries(block: str) -> set[str]:
        entries: set[str] = set()
        stack: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            connector = re.search(r"(├──|└──|\|--|`--|\+--|\\--)\s*", line)
            if connector:
                prefix = line[: connector.start()]
                depth = len(prefix.replace("│", " ").replace("|", " ")) // 4
                name = line[connector.end() :].strip().strip("`")
                name = name.split("  #", 1)[0].strip()
                if not name or name in {".", "./"}:
                    continue
                is_dir = name.endswith("/")
                name = name.rstrip("/")
                stack = stack[:depth]
                parts = [part for part in stack if part] + [name]
                if is_dir:
                    stack = parts
                else:
                    entries.add("/".join(parts))
                continue

            for match in re.finditer(r"(?<![\w.-])(\.?[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9][\w.-]*)", line):
                entry = match.group(1)
                entries.add(entry[2:] if entry.startswith("./") else entry)
        return entries

    @classmethod
    def _readme_refreshed_section_context(cls, current_text: str, old_text: str) -> dict[str, object]:
        target = cls._first_markdown_heading(old_text)
        if target is None:
            return {"status": "no_heading_in_old_text", "target_heading": None}

        headings = cls._markdown_heading_entries(current_text)
        matches = [heading for heading in headings if heading["normalized"] == target["normalized"]]
        if len(matches) != 1:
            return {
                "status": "heading_not_found" if not matches else "heading_not_unique",
                "target_heading": cls._public_heading(target),
                "matching_headings": [cls._public_heading(match) for match in matches],
            }

        match = matches[0]
        lines = current_text.splitlines(keepends=True)
        start = int(match["index"])
        end = len(lines)
        for candidate in headings:
            if int(candidate["index"]) <= start:
                continue
            if int(candidate["level"]) <= int(match["level"]):
                end = int(candidate["index"])
                break
        section = "".join(lines[start:end])
        return {
            "status": "matched",
            "target_heading": cls._public_heading(target),
            "current_heading": cls._public_heading(match),
            "start_line": start + 1,
            "end_line": end,
            "text": compact_text(section, 6000),
            "text_sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(),
        }

    @classmethod
    def _first_markdown_heading(cls, text: str) -> dict[str, object] | None:
        headings = cls._markdown_heading_entries(text)
        return headings[0] if headings else None

    @staticmethod
    def _markdown_heading_entries(text: str) -> list[dict[str, object]]:
        headings: list[dict[str, object]] = []
        for index, line in enumerate(text.splitlines()):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.strip())
            if not match:
                continue
            title = match.group(2).strip().rstrip("#").strip()
            headings.append(
                {
                    "line": index + 1,
                    "index": index,
                    "level": len(match.group(1)),
                    "text": title,
                    "markdown": f"{match.group(1)} {title}",
                    "normalized": re.sub(r"\s+", " ", title).casefold(),
                }
            )
        return headings

    @staticmethod
    def _public_heading(heading: dict[str, object]) -> dict[str, object]:
        return {
            "line": heading.get("line"),
            "level": heading.get("level"),
            "text": heading.get("text"),
            "markdown": heading.get("markdown"),
        }

    @staticmethod
    def _block_declares_compact_summary(block: str) -> bool:
        text = block.lower()
        has_summary_marker = any(marker in text for marker in ("summary", "compact", "overview", "concise"))
        has_incomplete_marker = any(
            marker in text
            for marker in (
                "not a complete",
                "not complete",
                "not the complete",
                "not exhaustive",
                "not a full",
                "not full",
            )
        )
        return has_summary_marker and has_incomplete_marker

    def _ensure_task_scope(self, task: AgentTask, proposal: PatchProposal) -> None:
        if not task.affected_files:
            return
        stats = self.file_tool.validate_patch(proposal)
        allowed = set(task.affected_files)
        outside = [path for path in stats.changed_files if path not in allowed]
        if outside:
            raise ToolError(f"patch touches files outside task scope: {outside}")

    def _ensure_structured_task_scope(self, task: AgentTask, proposal: StructuredEditProposal) -> None:
        if not task.affected_files:
            return
        allowed = set(task.affected_files)
        outside = [edit.path for edit in proposal.edits if edit.path not in allowed]
        if outside:
            raise ToolError(f"structured edit touches files outside task scope: {outside}")

    def _file_snippets(self, task: AgentTask) -> dict[str, str]:
        snippets: dict[str, str] = {}
        for path in task.affected_files[:10]:
            try:
                snippets[path] = compact_text(self.file_tool.read_file(path), 8_000)
            except Exception as exc:
                snippets[path] = f"<unreadable: {exc}>"
        return snippets

    @staticmethod
    def _task_text(task: AgentTask) -> str:
        return json.dumps(task.model_dump(mode="json"), ensure_ascii=False)

    @classmethod
    def _task_mentions_project_structure(cls, task: AgentTask) -> bool:
        text = cls._task_text(task)
        return bool(
            PROJECT_STRUCTURE_HEADING_RE.search(text)
            or re.search(r"\b(project|repository|file|directory)\s+structure\b", text, re.IGNORECASE)
            or re.search(r"\b(file tree|directory tree|repo tree|repository tree)\b", text, re.IGNORECASE)
            or ACTUAL_STRUCTURE_RE.search(text)
        )

    @classmethod
    def _task_allows_compact_structure_summary(cls, task: AgentTask) -> bool:
        return bool(COMPACT_SUMMARY_RE.search(cls._task_text(task)))

    @staticmethod
    def _is_docs_task(task: AgentTask) -> bool:
        if task.task_type == TaskType.DOCS:
            return True
        if not task.affected_files:
            return False
        return all(ImplementationAgent._is_docs_path(path) for path in task.affected_files) or any(
            ImplementationAgent._is_docs_path(path) for path in task.affected_files
        )

    @staticmethod
    def _is_docs_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        name = normalized.rsplit("/", 1)[-1]
        return normalized.startswith("docs/") or name.startswith("readme") or normalized.endswith((".md", ".markdown"))

    @staticmethod
    def _is_readme_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return normalized.rsplit("/", 1)[-1].startswith("readme")

    @staticmethod
    def _is_patch_fallback_candidate(exc: PatchApplyError | UnifiedDiffValidationError) -> bool:
        if isinstance(exc, UnifiedDiffValidationError):
            return True
        return "corrupt patch" in exc.stderr.lower()

    @staticmethod
    def _patch_failure_reason(exc: PatchApplyError | UnifiedDiffValidationError) -> str:
        if isinstance(exc, UnifiedDiffValidationError):
            return "patch_validation_failed"
        return "corrupt_patch" if "corrupt patch" in exc.stderr.lower() else "patch_apply_failed"

    @staticmethod
    def _structured_failure_reason(exc: Exception) -> str:
        if isinstance(exc, ProjectStructureValidationError):
            return exc.reason
        if isinstance(exc, StructuredEditError):
            return exc.reason
        text = str(exc).lower()
        if "outside task scope" in text:
            return "outside_task_scope"
        if "symlink" in text:
            return "symlink_rejected"
        if "old_text not found" in text:
            return "old_text_not_found"
        if "multiple times" in text:
            return "old_text_not_unique"
        if "protected" in text:
            return "protected_path"
        if "secret" in text:
            return "secret_detected"
        return "structured_edit_failed"

    @staticmethod
    def _structured_error_payload(exc: Exception) -> dict[str, object]:
        if isinstance(exc, ProjectStructureValidationError):
            return {
                "reason": exc.reason,
                "error": str(exc),
                "removed_existing_entries": exc.evidence.get("removed_existing_entries", []),
                "project_structure_evidence": exc.evidence,
            }
        if isinstance(exc, StructuredEditError):
            return exc.to_dict()
        return {
            "reason": ImplementationAgent._structured_failure_reason(exc),
            "error": str(exc),
        }

    @classmethod
    def _structured_apply_failure_message(cls, exc: Exception) -> str:
        if isinstance(exc, StructuredEditError) and exc.reason == "old_text_not_found":
            return (
                f"Structured edit old_text was not found in {exc.path}. "
                "AgentLab wrote structured_edit_anchor_error.json with the old_text hash, nearby candidate matches, "
                "and current README headings. Refresh the old_text from the current section or use insert_before/insert_after."
            )
        return str(exc)

    @classmethod
    def _structured_retry_failure_message(cls, original_exc: Exception, repair_exc: Exception) -> str:
        if isinstance(original_exc, StructuredEditError) and original_exc.reason == "old_text_not_found":
            repair_artifact = " and structured_edit_repair_error.json" if isinstance(repair_exc, StructuredEditError) else ""
            return (
                f"Structured edit repair failed after refreshing anchor context for {original_exc.path}. "
                f"Inspect structured_edit_anchor_error.json{repair_artifact}; refresh the replace_text old_text "
                "from the current README section or switch to insert_before/insert_after. "
                f"Last error: {cls._short_error(str(repair_exc))}"
            )
        return cls._structured_apply_failure_message(repair_exc)

    @staticmethod
    def _is_structured_repair_candidate(exc: Exception) -> bool:
        return ImplementationAgent._structured_failure_reason(exc) in {
            "old_text_not_found",
            "old_text_not_unique",
            "anchor_not_found",
            "anchor_not_unique",
        }

    @staticmethod
    def _is_git_author_identity_missing(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "author identity unknown" in text
            or "please tell me who you are" in text
            or "unable to auto-detect email address" in text
        )

    @staticmethod
    def _push_failure_reason(stderr: str) -> str:
        text = stderr.lower()
        if "non-fast-forward" in text or "fetch first" in text:
            return "non_fast_forward"
        return "push_failed"

    @staticmethod
    def _short_error(text: str, limit: int = 1200) -> str:
        compact = "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
        return compact[:limit]

    @staticmethod
    def _structured_normalized_attempt(raw_response: str) -> object | None:
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return payload
        normalized = dict(payload)
        edits = normalized.get("edits")
        if isinstance(edits, list):
            normalized_edits = []
            for edit in edits:
                if not isinstance(edit, dict):
                    normalized_edits.append(edit)
                    continue
                item = dict(edit)
                if "path" not in item:
                    for alias in ("file_path", "filepath", "filename", "file"):
                        if alias in item:
                            item["path"] = item[alias]
                            break
                if "operation" not in item:
                    for alias in ("tool", "op", "action"):
                        if alias in item:
                            item["operation"] = item[alias]
                            break
                normalized_edits.append(item)
            normalized["edits"] = normalized_edits
        return normalized

    @staticmethod
    def _structured_schema_message(raw_response: str) -> str:
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError:
            return "StructuredEditProposal schema validation failed: response was not valid JSON"
        edits = payload.get("edits") if isinstance(payload, dict) else None
        if isinstance(edits, list):
            for index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    continue
                used = []
                if any(alias in edit for alias in ("file_path", "filepath", "filename", "file")) and "path" not in edit:
                    used.append("file_path")
                if any(alias in edit for alias in ("tool", "op", "action")) and "operation" not in edit:
                    used.append("tool")
                if used:
                    return (
                        "StructuredEditProposal schema validation failed: "
                        f"edits[{index}] used {'/'.join(used)}; expected path/operation"
                    )
        return "StructuredEditProposal schema validation failed: expected path/operation in each edit"

    @staticmethod
    def _is_corrupt_patch(stderr: str) -> bool:
        return "corrupt patch" in stderr.lower()
