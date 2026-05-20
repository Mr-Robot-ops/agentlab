from __future__ import annotations

import json
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.models import AgentTask, ImplementationReport, PatchProposal, ReportStatus
from agentlab.policies.risk import assess_risk
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import FileTool, PatchApplyError
from agentlab.tools.git_tool import GitTool
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


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
        if not task.approved:
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["task is not approved for implementation"],
                no_changes_committed=True,
                no_branch_pushed=True,
            )

        try:
            checkout = self.git_tool.create_branch(branch, self.config.default_branch)
            if not checkout.ok:
                raise ToolError(checkout.stderr or "could not create agent branch")
            proposal, raw_response = self._proposal(task)
            patch_artifacts.extend(self._persist_patch_inputs(raw_response, proposal))
            try:
                self._ensure_task_scope(task, proposal)
                diff_stats = self.file_tool.apply_patch(proposal)
            except PatchApplyError as exc:
                patch_artifacts.extend(self._persist_patch_apply_error(exc))
                if not self._is_corrupt_patch(exc.stderr):
                    raise
                retry_attempted = True
                repaired, repaired_raw = self._repair_patch(task, proposal.patch, exc.stderr)
                patch_artifacts.extend(self._persist_patch_inputs(repaired_raw, repaired, prefix="repair_"))
                self._ensure_task_scope(task, repaired)
                try:
                    diff_stats = self.file_tool.apply_patch(repaired)
                    proposal = repaired
                    retry_succeeded = True
                except PatchApplyError as retry_exc:
                    patch_artifacts.extend(self._persist_patch_apply_error(retry_exc, prefix="repair_"))
                    raise retry_exc
            risk = assess_risk(task, diff_stats.changed_files, proposal.patch)
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
                patch_summary=proposal.summary,
                changed_files=diff_stats.changed_files,
                risk_score=risk.score,
                tests_recommended=proposal.expected_tests,
                patch_artifacts=patch_artifacts,
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
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
                retry_attempted=retry_attempted,
                retry_succeeded=retry_succeeded,
                no_changes_committed=not commit_created,
                no_branch_pushed=not branch_pushed,
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

    def _repair_patch(self, task: AgentTask, original_patch: str, stderr: str) -> tuple[PatchProposal, str]:
        payload = {
            "task": task.model_dump(mode="json"),
            "original_patch": original_patch,
            "git_apply_stderr": stderr,
            "rules": {
                "repair_only": "Repair only unified diff syntax/format so git apply can parse it.",
                "scope": "Do not change task scope or intent.",
                "allowed_files": task.affected_files,
                "forbidden_actions": task.forbidden_actions,
                "output": "Return one PatchProposal JSON object with the repaired patch.",
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

    def _ensure_task_scope(self, task: AgentTask, proposal: PatchProposal) -> None:
        if not task.affected_files:
            return
        stats = self.file_tool.validate_patch(proposal)
        allowed = set(task.affected_files)
        outside = [path for path in stats.changed_files if path not in allowed]
        if outside:
            raise ToolError(f"patch touches files outside task scope: {outside}")

    @staticmethod
    def _is_corrupt_patch(stderr: str) -> bool:
        return "corrupt patch" in stderr.lower()
