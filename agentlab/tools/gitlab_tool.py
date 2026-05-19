from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

from agentlab.config import AppConfig
from agentlab.models import MergeRequestInfo


MERGEABLE_DETAILED_STATUSES = {"mergeable", "can_be_merged"}
MERGEABLE_STATUSES = {"can_be_merged"}


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

    def find_open_mr(self, source_branch: str, target_branch: str) -> MergeRequestInfo | None:
        mrs = self.project.mergerequests.list(
            state="opened",
            source_branch=source_branch,
            target_branch=target_branch,
            per_page=1,
        )
        if not mrs:
            return None
        return self._mr_info(self.project.mergerequests.get(mrs[0].iid))

    def create_or_update_mr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str],
    ) -> MergeRequestInfo:
        existing = self.find_open_mr(source_branch, target_branch)
        if existing is not None:
            return self.update_mr(existing.iid or existing.mr_id, title=title, description=description, labels=",".join(labels))
        return self.create_mr(
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            labels=labels,
        )

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

    def add_labels_to_mr(self, mr_id: int, labels: list[str]) -> MergeRequestInfo:
        mr = self.project.mergerequests.get(mr_id)
        current = list(getattr(mr, "labels", []) or [])
        merged = current[:]
        for label in labels:
            if label not in merged:
                merged.append(label)
        mr.labels = ",".join(merged)
        mr.save()
        return self._mr_info(mr)

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

    def get_mr_pipeline_status(self, mr_iid: int) -> dict[str, Any]:
        mr = self.project.mergerequests.get(mr_iid)
        head_pipeline = getattr(mr, "head_pipeline", None)
        if isinstance(head_pipeline, dict) and head_pipeline.get("id"):
            pipeline = self.project.pipelines.get(head_pipeline["id"])
            return {
                "id": pipeline.id,
                "status": pipeline.status,
                "web_url": getattr(pipeline, "web_url", None),
                "sha": getattr(pipeline, "sha", None),
            }
        return self.get_pipeline_status(getattr(mr, "source_branch", None))

    def wait_for_pipeline(
        self,
        *,
        ref: str | None = None,
        mr_iid: int | None = None,
        timeout_seconds: int = 600,
        poll_seconds: int = 10,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        latest: dict[str, Any] = {"status": "missing"}
        while time.monotonic() < deadline:
            latest = self.get_mr_pipeline_status(mr_iid) if mr_iid is not None else self.get_pipeline_status(ref)
            if latest.get("status") in {"success", "failed", "canceled", "skipped", "manual"}:
                return latest
            time.sleep(poll_seconds)
        latest["timed_out"] = True
        return latest

    def trigger_pipeline(self, ref: str) -> dict[str, Any]:
        pipeline = self.project.pipelines.create({"ref": ref})
        return {"id": pipeline.id, "status": pipeline.status, "web_url": getattr(pipeline, "web_url", None)}

    def get_mr_approval_state(self, mr_iid: int) -> dict[str, Any]:
        project_id = quote(str(self.config.project_id), safe="")
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approval_state")

    def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]:
        project_id = quote(str(self.config.project_id), safe="")
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approvals")

    def get_mr_merge_readiness(self, mr_id: int) -> dict[str, Any]:
        mr = self.project.mergerequests.get(mr_id)
        return {
            "state": getattr(mr, "state", None),
            "draft": bool(getattr(mr, "draft", False)),
            "has_conflicts": bool(getattr(mr, "has_conflicts", False)),
            "detailed_merge_status": getattr(mr, "detailed_merge_status", None),
            "merge_status": getattr(mr, "merge_status", None),
        }

    def merge_mr(self, mr_id: int, *, squash: bool = True) -> MergeRequestInfo:
        mr = self.project.mergerequests.get(mr_id)
        mr.merge(squash=squash)
        return self._mr_info(mr)

    def merge_mr_guarded(self, mr_id: int, *, squash: bool = True) -> MergeRequestInfo:
        mr = self.project.mergerequests.get(mr_id)
        self._assert_mr_mergeable(mr)
        mr.merge(squash=squash)
        refreshed = self.project.mergerequests.get(mr_id)
        return self._mr_info(refreshed)

    @staticmethod
    def _assert_mr_mergeable(mr: Any) -> None:
        state = getattr(mr, "state", None)
        if state and state != "opened":
            raise RuntimeError(f"refusing to merge MR with state: {state}")
        if getattr(mr, "draft", False):
            raise RuntimeError("refusing to merge draft MR")
        if getattr(mr, "has_conflicts", False):
            raise RuntimeError("refusing to merge MR with conflicts")
        detailed_status = getattr(mr, "detailed_merge_status", None)
        merge_status = getattr(mr, "merge_status", None)
        if detailed_status:
            if detailed_status not in MERGEABLE_DETAILED_STATUSES:
                raise RuntimeError(f"MR detailed_merge_status is not mergeable: {detailed_status}")
            return
        if merge_status:
            if merge_status not in MERGEABLE_STATUSES:
                raise RuntimeError(f"MR merge_status is not mergeable: {merge_status}")
            return
        raise RuntimeError("MR mergeability is unknown")

    def get_latest_pipeline_jobs(self, ref: str, *, per_page: int = 20) -> list[dict[str, Any]]:
        pipeline_status = self.get_latest_pipeline_status(ref)
        pipeline_id = pipeline_status.get("id")
        if not pipeline_id:
            return []
        pipeline = self.project.pipelines.get(pipeline_id)
        return [job.asdict() for job in pipeline.jobs.list(per_page=per_page)]

    def get_job_log_excerpt(self, job_id: int, *, max_chars: int = 4000) -> str:
        job = self.project.jobs.get(job_id)
        trace = job.trace()
        text = trace.decode("utf-8", errors="replace") if isinstance(trace, bytes) else str(trace)
        return text[-max_chars:]

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
