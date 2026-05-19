from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from agentlab.config import AppConfig
from agentlab.models import MergeRequestInfo


class GitLabTool:
    def __init__(self, config: AppConfig) -> None:
        try:
            import gitlab  # type: ignore
        except ImportError as exc:
            raise RuntimeError("python-gitlab is required for GitLab operations") from exc

        token = os.environ.get(config.gitlab_token_env)
        if not token:
            raise RuntimeError(f"GitLab token env var is not set: {config.gitlab_token_env}")
        self.config = config
        self.client = gitlab.Gitlab(config.gitlab_url, private_token=token)
        self.project = self.client.projects.get(config.project_id)

    def create_mr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str],
    ) -> MergeRequestInfo:
        mr = self.project.mergerequests.create(
            {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "labels": ",".join(labels),
            }
        )
        return self._mr_info(mr)

    def update_mr(self, mr_id: int, **updates: Any) -> MergeRequestInfo:
        mr = self.project.mergerequests.get(mr_id)
        for key, value in updates.items():
            setattr(mr, key, value)
        mr.save()
        return self._mr_info(mr)

    def comment_mr(self, mr_id: int, body: str) -> None:
        mr = self.project.mergerequests.get(mr_id)
        mr.notes.create({"body": body})

    def get_pipeline_status(self, ref: str | None = None) -> dict[str, Any]:
        pipelines = self.project.pipelines.list(ref=ref, per_page=1) if ref else self.project.pipelines.list(per_page=1)
        if not pipelines:
            return {"status": "missing"}
        pipeline = self.project.pipelines.get(pipelines[0].id)
        return {"id": pipeline.id, "status": pipeline.status, "web_url": getattr(pipeline, "web_url", None)}

    def get_latest_pipeline_status(self, ref: str) -> dict[str, Any]:
        project_id = quote(str(self.config.project_id), safe="")
        try:
            pipeline = self.client.http_get(f"/projects/{project_id}/pipelines/latest", query_data={"ref": ref})
        except Exception as exc:
            return {"status": "missing", "error": str(exc)}
        return {
            "id": pipeline.get("id"),
            "status": pipeline.get("status"),
            "web_url": pipeline.get("web_url"),
            "ref": pipeline.get("ref"),
            "sha": pipeline.get("sha"),
        }

    def trigger_pipeline(self, ref: str) -> dict[str, Any]:
        pipeline = self.project.pipelines.create({"ref": ref})
        return {"id": pipeline.id, "status": pipeline.status, "web_url": getattr(pipeline, "web_url", None)}

    def get_mr_approval_state(self, mr_iid: int) -> dict[str, Any]:
        project_id = quote(str(self.config.project_id), safe="")
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approval_state")

    def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]:
        project_id = quote(str(self.config.project_id), safe="")
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approvals")

    def merge_mr(self, mr_id: int, *, squash: bool = True) -> MergeRequestInfo:
        mr = self.project.mergerequests.get(mr_id)
        mr.merge(squash=squash)
        return self._mr_info(mr)

    def list_issues(self, *, state: str = "opened", per_page: int = 20) -> list[dict[str, Any]]:
        return [issue.asdict() for issue in self.project.issues.list(state=state, per_page=per_page)]

    def _mr_info(self, mr: Any) -> MergeRequestInfo:
        labels = getattr(mr, "labels", []) or []
        return MergeRequestInfo(
            mr_id=int(mr.id),
            iid=int(getattr(mr, "iid", mr.id)),
            title=mr.title,
            web_url=getattr(mr, "web_url", None),
            source_branch=mr.source_branch,
            target_branch=mr.target_branch,
            labels=list(labels),
        )
