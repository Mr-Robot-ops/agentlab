from __future__ import annotations

import os
import time
from typing import Any

from agentlab.config import AppConfig, gitlab_project_api_id
from agentlab.models import MergeRequestInfo


MERGEABLE_DETAILED_STATUSES = {"mergeable", "can_be_merged"}
MERGEABLE_STATUSES = {"can_be_merged"}
GITLAB_ACCESS_ROLES = {
    10: "guest",
    20: "reporter",
    30: "developer",
    40: "maintainer",
    50: "owner",
}


class GitLabTool:
    def __init__(self, config: AppConfig, *, token: str | None = None) -> None:
        try:
            import gitlab  # type: ignore
        except ImportError as exc:
            raise RuntimeError("python-gitlab is required for GitLab operations") from exc

        token = token or os.environ.get(config.gitlab_token_env)
        if not token:
            raise RuntimeError(f"GitLab token env var is not set: {config.gitlab_token_env}")
        self.config = config
        self.client = gitlab.Gitlab(config.gitlab_url, private_token=token)
        self.project = self.client.projects.get(gitlab_project_api_id(config.project_id))

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

    def post_merge_request_note(self, mr_iid: int, body: str) -> dict[str, Any]:
        mr = self.project.mergerequests.get(mr_iid)
        note = mr.notes.create({"body": body})
        return _asdict(note)

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
        project_id = gitlab_project_api_id(self.config.project_id)
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
        project_id = gitlab_project_api_id(self.config.project_id)
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approval_state")

    def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]:
        project_id = gitlab_project_api_id(self.config.project_id)
        return self.client.http_get(f"/projects/{project_id}/merge_requests/{mr_iid}/approvals")

    def get_mr_merge_readiness(self, mr_id: int) -> dict[str, Any]:
        mr = self.project.mergerequests.get(mr_id)
        return {
            "state": getattr(mr, "state", None),
            "draft": bool(getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False)),
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
        if state != "opened":
            raise RuntimeError(f"refusing to merge MR with state: {state}")
        if getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False):
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

    def get_default_branch_head(self) -> str | None:
        branch = self.project.branches.get(self.config.default_branch)
        commit = getattr(branch, "commit", None)
        if isinstance(commit, dict):
            return commit.get("id") or commit.get("sha")
        return getattr(commit, "id", None) or getattr(commit, "sha", None)

    def list_open_agent_mrs(self) -> list[MergeRequestInfo]:
        mrs = self.project.mergerequests.list(state="opened", target_branch=self.config.default_branch, all=True)
        result = []
        for mr in mrs:
            source_branch = getattr(mr, "source_branch", "")
            if str(source_branch).startswith("agent/"):
                result.append(self._mr_info(mr))
        return result

    def list_open_agent_merge_requests(self) -> list[MergeRequestInfo]:
        mrs = self.project.mergerequests.list(
            state="opened",
            target_branch=self.config.default_branch,
            labels="agent/generated",
            all=True,
        )
        result: list[MergeRequestInfo] = []
        for mr in mrs:
            info = self._mr_info(mr)
            if (
                info.source_branch.startswith("agent/")
                and info.target_branch == self.config.default_branch
                and "agent/generated" in info.labels
            ):
                result.append(info)
        return result

    def list_agent_merge_requests(self, *, state: str = "opened", label: str = "agent/generated") -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "state": state,
            "target_branch": self.config.default_branch,
            "all": True,
        }
        if label:
            kwargs["labels"] = label
        mrs = self.project.mergerequests.list(**kwargs)
        result: list[dict[str, Any]] = []
        for mr in mrs:
            info = self._mr_info(mr)
            if not info.source_branch.startswith("agent/"):
                continue
            if info.target_branch != self.config.default_branch:
                continue
            if label and label not in info.labels:
                continue
            result.append(
                {
                    "iid": info.iid,
                    "title": info.title,
                    "state": str(getattr(mr, "state", state)),
                    "source_branch": info.source_branch,
                    "web_url": info.web_url,
                    "labels": info.labels,
                    "updated_at": info.updated_at,
                    "closed_at": getattr(mr, "closed_at", None),
                    "merged_at": getattr(mr, "merged_at", None),
                }
            )
        return result

    def list_closed_agent_merge_requests(self, *, label: str = "agent/generated") -> list[dict[str, Any]]:
        return self.list_agent_merge_requests(state="closed", label=label)

    def get_merge_request(self, mr_iid: int) -> MergeRequestInfo:
        return self._mr_info(self.project.mergerequests.get(mr_iid))

    def list_merge_request_notes(self, mr_iid: int) -> list[dict[str, Any]]:
        mr = self.project.mergerequests.get(mr_iid)
        return [_asdict(note) for note in mr.notes.list(all=True)]

    def list_merge_request_discussions(self, mr_iid: int) -> list[dict[str, Any]]:
        mr = self.project.mergerequests.get(mr_iid)
        discussions = getattr(mr, "discussions", None)
        if discussions is None:
            return []
        return [_asdict(discussion) for discussion in discussions.list(all=True)]

    def get_merge_request_changes(self, mr_iid: int) -> list[str]:
        mr = self.project.mergerequests.get(mr_iid)
        changes = mr.changes()
        return [
            str(change.get("new_path") or change.get("old_path"))
            for change in changes.get("changes", [])
            if change.get("new_path") or change.get("old_path")
        ]

    def get_current_user(self) -> dict[str, Any]:
        user = getattr(self.client, "user", None)
        if user is None:
            self.client.auth()
            user = getattr(self.client, "user", None)
        return _asdict(user) if user is not None else {}

    def get_project_member_role(self, user_id: int) -> dict[str, Any]:
        member = None
        members_all = getattr(self.project, "members_all", None)
        if members_all is not None:
            try:
                member = members_all.get(user_id)
            except Exception:
                member = None
        if member is None:
            member = self.project.members.get(user_id)
        access_level = int(getattr(member, "access_level", 0) or 0)
        return {"access_level": access_level, "role": GITLAB_ACCESS_ROLES.get(access_level, "unknown")}

    def author_is_allowed(
        self,
        author: dict[str, Any],
        *,
        allowed_authors: list[str],
        require_author_role: list[str],
    ) -> bool:
        username = str(author.get("username") or "").lower()
        if username and username in {item.lower() for item in allowed_authors}:
            return True
        user_id = author.get("id")
        if user_id is None or not require_author_role:
            return False
        try:
            role = self.get_project_member_role(int(user_id)).get("role")
        except Exception:
            return False
        return str(role).lower() in {item.lower() for item in require_author_role}

    def _mr_info(self, mr: Any) -> MergeRequestInfo:
        labels = getattr(mr, "labels", []) or []
        if isinstance(labels, str):
            labels = [label.strip() for label in labels.split(",") if label.strip()]
        return MergeRequestInfo(
            mr_id=int(mr.id),
            iid=int(getattr(mr, "iid", mr.id)),
            title=mr.title,
            web_url=getattr(mr, "web_url", None),
            source_branch=mr.source_branch,
            target_branch=mr.target_branch,
            labels=list(labels),
            updated_at=getattr(mr, "updated_at", None),
        )


def _asdict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "asdict"):
        return value.asdict()
    if hasattr(value, "attributes"):
        attributes = getattr(value, "attributes")
        if isinstance(attributes, dict):
            return dict(attributes)
    result: dict[str, Any] = {}
    for key in ("id", "iid", "body", "author", "created_at", "updated_at", "system", "notes", "username", "name", "access_level"):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result
