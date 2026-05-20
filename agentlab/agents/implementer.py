from __future__ import annotations

import json
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.models import AgentTask, DiffStats, ImplementationReport, PatchProposal, ReportStatus, StructuredEditProposal, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import FileTool, PatchApplyError, UnifiedDiffValidationError
from agentlab.tools.git_tool import GitTool
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


class StructuredEditApplyError(ToolError):
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
    ) -> None:
        self.config = config
        self.git_tool = git_tool
        self.file_tool = file_tool
        self.ollama = ollama
        self.dry_run = dry_run
        self.repo_context = repo_context or {}
        self.artifacts = artifacts

    def implement(self, task: AgentTask) -> ImplementationReport:
        branch = f"agent/{task.id}"
        errors: list[str] = []
        patch_artifacts: list[str] = []
        retry_attempted = False
        retry_succeeded = False
        commit_created = False
        branch_pushed = False
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
                structured, raw_response = self._structured_proposal(task)
                patch_artifacts.extend(self._persist_structured_inputs(raw_response, structured))
                try:
                    diff_stats = self._validate_and_apply_structured(task, structured)
                except Exception as structured_exc:
                    patch_artifacts.extend(self._persist_structured_error(structured_exc))
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
                        structured, structured_raw = self._structured_proposal(task, fallback_reason=fallback_reason)
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
            pushed = False
            if self.config.push_agent_branches_enabled and not self.dry_run:
                push = self.git_tool.push(branch)
                if not push.ok:
                    raise ToolError(push.stderr or "git push failed")
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
        except Exception as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
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
                "scope": "Only edit files listed in task.affected_files.",
                "operations": {
                    "replace_text": "Use when replacing exactly one existing text block; old_text must match exactly once.",
                    "append_to_file": "Use only when appending to an existing file.",
                    "replace_file": "Use only when full target file content is safer than a small replacement.",
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

    def _persist_structured_inputs(self, raw_response: str, proposal: StructuredEditProposal) -> list[str]:
        if self.artifacts is None:
            return []
        records = [
            self.artifacts.write_text("structured_edit_raw_response.json", raw_response),
            self.artifacts.write_json("structured_edit_proposal", proposal),
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
    ) -> list[str]:
        if self.artifacts is None:
            return []
        record = self.artifacts.write_json(
            "structured_edit_apply_report",
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

    def _persist_structured_error(self, exc: Exception, *, fallback_reason: str | None = None) -> list[str]:
        if self.artifacts is None:
            return []
        record = self.artifacts.write_json(
            "structured_edit_error",
            {
                "status": "failed",
                "error": str(exc),
                "reason": self._structured_failure_reason(exc),
                "fallback_reason": fallback_reason,
            },
        )
        return [record.name]

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
    def _is_corrupt_patch(stderr: str) -> bool:
        return "corrupt patch" in stderr.lower()
