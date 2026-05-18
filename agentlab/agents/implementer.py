from __future__ import annotations

import json
from typing import Any

from agentlab.config import AppConfig
from agentlab.models import AgentTask, ImplementationReport, PatchProposal, ReportStatus
from agentlab.policies.risk import assess_risk
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import FileTool
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
    ) -> None:
        self.config = config
        self.git_tool = git_tool
        self.file_tool = file_tool
        self.ollama = ollama
        self.dry_run = dry_run
        self.repo_context = repo_context or {}

    def implement(self, task: AgentTask) -> ImplementationReport:
        branch = f"agent/{task.id}"
        errors: list[str] = []
        if not task.approved:
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=["task is not approved for implementation"],
            )

        try:
            checkout = self.git_tool.create_branch(branch, self.config.default_branch)
            if not checkout.ok:
                raise ToolError(checkout.stderr or "could not create agent branch")
            proposal = self._proposal(task)
            diff_stats = self.file_tool.apply_patch(proposal)
            risk = assess_risk(task, diff_stats.changed_files, proposal.patch)
            if risk.blocked:
                raise ToolError("risk assessment blocked patch: " + ", ".join(risk.reasons))
            commit_sha = self.git_tool.commit(f"agent: {task.title}")
            pushed = False
            if self.config.push_agent_branches_enabled and not self.dry_run:
                push = self.git_tool.push(branch)
                if not push.ok:
                    raise ToolError(push.stderr or "git push failed")
                pushed = True
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
            )
        except Exception as exc:
            errors.append(str(exc))
            return ImplementationReport(
                task_id=task.id,
                branch=branch,
                status=ReportStatus.FAILED,
                errors=errors,
            )

    def _proposal(self, task: AgentTask) -> PatchProposal:
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
        proposal = self.ollama.chat_json(
            model=self.config.agent_model("implementer"),
            system_prompt=load_prompt("implementer.md"),
            user_prompt=json.dumps(payload, indent=2),
            response_model=PatchProposal,
        )
        if proposal.task_id != task.id:
            raise ToolError("PatchProposal.task_id does not match task id")
        return proposal
