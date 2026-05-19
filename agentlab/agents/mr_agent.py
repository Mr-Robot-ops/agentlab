from __future__ import annotations

from agentlab.config import AppConfig
from agentlab.models import AgentTask, ImplementationReport, MergeRequestInfo, RiskLevel
from agentlab.tools.gitlab_tool import GitLabTool


class MergeRequestAgent:
    name = "mr_agent"

    def __init__(self, config: AppConfig, gitlab_tool: GitLabTool) -> None:
        self.config = config
        self.gitlab_tool = gitlab_tool

    def create_or_update(
        self,
        *,
        task: AgentTask,
        implementation: ImplementationReport,
        mr_id: int | None = None,
    ) -> MergeRequestInfo:
        title = f"[agent] {task.title}"
        description = self.description(task, implementation)
        labels = self.labels(task)
        if mr_id is not None:
            return self.gitlab_tool.update_mr(mr_id, title=title, description=description, labels=",".join(labels))
        return self.gitlab_tool.create_or_update_mr(
            source_branch=implementation.branch,
            target_branch=self.config.default_branch,
            title=title,
            description=description,
            labels=labels,
        )

    def description(self, task: AgentTask, implementation: ImplementationReport) -> str:
        checklist = "\n".join(
            [
                "- [ ] Functional tests passed",
                "- [ ] Build/security checks passed",
                "- [ ] Quality review approved",
                "- [ ] Security/architecture review approved",
                "- [ ] Gatekeeper approved merge",
            ]
        )
        changed = "\n".join(f"- `{path}`" for path in implementation.changed_files) or "- No files changed"
        tests = "\n".join(f"- `{cmd}`" for cmd in implementation.tests_recommended) or "- Not specified"
        return (
            f"## Summary\n{task.description or implementation.patch_summary}\n\n"
            f"## Changed Files\n{changed}\n\n"
            f"## Risk\nRisk score: `{implementation.risk_score or task.risk_score}`\n"
            f"Risk level: `{task.risk_level}`\n\n"
            f"## Tests\n{tests}\n\n"
            f"## Rollback\nRevert commit `{implementation.commit_sha or '<pending>'}` or close this MR before merge.\n\n"
            f"## Checklist\n{checklist}\n"
        )

    def labels(self, task: AgentTask) -> list[str]:
        labels = ["agent/generated", f"risk/{task.risk_level}"]
        if task.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL) or task.task_type.value in {"security", "auth"}:
            labels.append("agent/security")
        return labels
