from __future__ import annotations

from agentlab.config import AppConfig
from agentlab.models import ReportStatus, RollbackReport
from agentlab.tools.git_tool import GitTool
from agentlab.tools.gitlab_tool import GitLabTool


class RollbackRecoveryAgent:
    name = "rollback"

    def __init__(self, config: AppConfig, git_tool: GitTool, gitlab_tool: GitLabTool | None = None) -> None:
        self.config = config
        self.git_tool = git_tool
        self.gitlab_tool = gitlab_tool

    def recover(self, *, ref: str | None = None, commit_sha: str | None = None, auto_revert: bool = False) -> RollbackReport:
        pipeline = self.gitlab_tool.get_pipeline_status(ref) if self.gitlab_tool else {"status": "unknown"}
        status = pipeline.get("status")
        if status not in {"failed", "canceled"}:
            return RollbackReport(
                status=ReportStatus.SKIPPED,
                commit_sha=commit_sha,
                pipeline_status=str(status),
                incident_summary="No failed pipeline requiring recovery was found.",
                recommended_action="Continue monitoring.",
            )
        if not commit_sha:
            return RollbackReport(
                status=ReportStatus.FAILED,
                pipeline_status=str(status),
                incident_summary="Pipeline failed but no commit SHA was provided for revert planning.",
                recommended_action="Identify the merge commit and run recover with commit_sha.",
            )
        branch = f"agent/revert-{commit_sha[:12]}"
        self.git_tool.create_branch(branch, self.config.default_branch)
        if auto_revert:
            result = self.git_tool.revert(commit_sha)
            if not result.ok:
                return RollbackReport(
                    status=ReportStatus.FAILED,
                    commit_sha=commit_sha,
                    pipeline_status=str(status),
                    revert_branch=branch,
                    incident_summary=result.stderr,
                    recommended_action="Manual revert required.",
                )
            revert_sha = self.git_tool.commit(f"agent: revert {commit_sha}")
        else:
            self.git_tool.revert(commit_sha, no_commit=True)
            revert_sha = None
        return RollbackReport(
            status=ReportStatus.PASSED,
            commit_sha=commit_sha,
            pipeline_status=str(status),
            revert_branch=branch,
            revert_commit_sha=revert_sha,
            incident_summary=f"Pipeline for {ref or commit_sha} failed with status {status}.",
            recommended_action="Review the revert branch and open a recovery MR.",
        )
