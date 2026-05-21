from __future__ import annotations

import json
import uuid
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.branching import agent_branch_name
from agentlab.config import AppConfig
from agentlab.models import AgentTask, DiffStats, ImplementationReport, PatchProposal, ReportStatus, StructuredEditProposal, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import FileTool, PatchApplyError, StructuredEditError, UnifiedDiffValidationError
from agentlab.tools.git_tool import GitTool
from agentlab.tools.ollama_client import OllamaClient, OllamaSchemaValidationError

from .base import compact_text, load_prompt


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
                    diff_stats = self._validate_and_apply_structured(task, structured)
                except Exception as structured_exc:
                    patch_artifacts.extend(self._persist_structured_error(structured_exc))
                    if self._is_structured_repair_candidate(structured_exc):
                        retry_attempted = True
                        try:
                            repaired, repaired_raw = self._repair_structured_proposal(task, structured, structured_exc)
                            patch_artifacts.extend(self._persist_structured_inputs(repaired_raw, repaired, repair=True))
                            diff_stats = self._validate_and_apply_structured(task, repaired)
                        except Exception as repair_exc:
                            patch_artifacts.extend(self._persist_structured_error(repair_exc, repair=True))
                            raise StructuredEditApplyError(self._structured_failure_reason(repair_exc), str(repair_exc)) from repair_exc
                        patch_artifacts.extend(self._persist_structured_apply_report(repaired, diff_stats, repair=True))
                        structured = repaired
                        retry_succeeded = True
                    else:
                        raise StructuredEditApplyError(self._structured_failure_reason(structured_exc), str(structured_exc)) from structured_exc
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
                            diff_stats = self._validate_and_apply_structured(task, structured)
                        except Exception as structured_exc:
                            patch_artifacts.extend(self._persist_structured_error(structured_exc, fallback_reason=fallback_reason))
                            raise StructuredEditApplyError(self._structured_failure_reason(structured_exc), str(structured_exc)) from structured_exc
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
                    diff_stats = self._validate_and_apply_structured(task, structured)
                except Exception as exc:
                    patch_artifacts.extend(self._persist_structured_error(exc))
                    raise StructuredEditApplyError(self._structured_failure_reason(exc), str(exc)) from exc
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
        payload = {
            "task": task.model_dump(mode="json"),
            "original_structured_edit_proposal": proposal.model_dump(mode="json"),
            "structured_edit_error": self._structured_error_payload(exc),
            "rules": {
                "output": "Return only one StructuredEditProposal JSON object.",
                "scope": "only repair anchors or old_text/new_text for the same affected files.",
                "same_affected_files": task.affected_files,
                "do_not_broaden_scope": True,
                "prefer_insert_operations": "For new Markdown sections, prefer insert_before or insert_after over replacing large blocks.",
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

    def _validate_and_apply_structured(self, task: AgentTask, proposal: StructuredEditProposal) -> DiffStats:
        self._ensure_structured_task_scope(task, proposal)
        return self.file_tool.apply_structured_edits(proposal)

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
        if isinstance(exc, StructuredEditError):
            return exc.to_dict()
        return {
            "reason": ImplementationAgent._structured_failure_reason(exc),
            "error": str(exc),
        }

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
