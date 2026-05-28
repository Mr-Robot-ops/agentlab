from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentlab.audit import redact_secrets
from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.orchestrator import Orchestrator
from agentlab.policies.auto_approval import AutoApprovalPolicy
from agentlab.review_comments import (
    author_id,
    author_username,
    flatten_merge_request_comments,
    is_agent_generated_mr,
    is_bot_author,
    mr_key,
    normalize_mr,
    parse_review_command,
    review_comment_key,
)
from agentlab.models import AgentTask, TaskPlan
from agentlab.tools.gitlab_tool import GitLabTool


STATE_DEFAULTS: dict[str, Any] = {
    "last_watch_run": None,
    "last_plan_run": None,
    "last_plan_run_id": None,
    "last_action_run": None,
    "last_default_branch_head": None,
    "new_mrs_today": 0,
    "new_mrs_date": None,
    "open_agent_mrs": 0,
    "open_agent_mrs_details": [],
    "closed_agent_mr_feedback": [],
    "cooldown_until": None,
    "last_selected_task_id": None,
    "last_review_comment_run": None,
    "processed_review_comments": {},
    "review_comments_seen": {},
    "stopped_mrs": {},
    "review_comment_cooldowns": {},
}

STOP_REASON_RE = re.compile(r"^\s*reason\s*:\s*(?P<reason>.+)", re.IGNORECASE | re.DOTALL)
STOP_REASON_INLINE_RE = re.compile(
    r"(?:^|\s)(?:/agent|@agentlab)\s+stop\s+reason\s*:\s*(?P<reason>.+)",
    re.IGNORECASE | re.DOTALL,
)


class SchedulerStateStore:
    def __init__(self, workspace_root: str | Path) -> None:
        self.path = Path(workspace_root) / "scheduler" / "state.json"

    def read(self) -> tuple[dict[str, Any], str | None]:
        if not self.path.exists():
            return deepcopy(STATE_DEFAULTS), None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            return deepcopy(STATE_DEFAULTS), f"state_file_invalid: {exc}"
        state = {**deepcopy(STATE_DEFAULTS), **{key: raw.get(key) for key in STATE_DEFAULTS if key in raw}}
        return state, None

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: state.get(key) for key in STATE_DEFAULTS}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
        os.replace(tmp, self.path)


class Scheduler:
    def __init__(self, config: AppConfig, *, run_id: str | None = None) -> None:
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex
        self.run_dir = Path(config.workspace_root) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._orchestrator: Orchestrator | None = None
        self.state_store = SchedulerStateStore(config.workspace_root)
        self.artifacts = ArtifactStore(self.run_dir, self.run_id)

    def watch(self) -> dict[str, Any]:
        if not self.config.schedule.enabled:
            return self._write_report(self._report("skipped", "schedule_disabled"))
        if not self.config.schedule.watch.enabled:
            return self._write_report(self._report("skipped", "watch_disabled"))
        state, warning = self.state_store.read()
        try:
            gitlab = self._gitlab()
            head = gitlab.get_default_branch_head()
        except Exception as exc:
            stale_count = int(state.get("open_agent_mrs") or 0)
            return self._write_report(
                self._report(
                    "failed",
                    "gitlab_unavailable",
                    error=_safe_error(exc),
                    state_warning=warning,
                    open_agent_mrs_count=stale_count,
                    open_agent_mrs=[],
                    open_agent_mrs_warning="using last known open_agent_mrs count because GitLab API failed",
                )
            )
        open_mr_details: list[dict[str, Any]] = []
        open_mrs_warning = None
        try:
            open_mr_details = _open_agent_mr_details(gitlab.list_open_agent_mrs())
            open_mrs = len(open_mr_details)
        except Exception as exc:
            open_mrs = int(state.get("open_agent_mrs") or 0)
            open_mr_details = list(state.get("open_agent_mrs_details") or [])
            open_mrs_warning = f"using last known open_agent_mrs count because GitLab API failed: {_safe_error(exc)}"
        closed_mr_feedback = list(state.get("closed_agent_mr_feedback") or [])
        closed_mrs_warning = None
        try:
            closed_mr_feedback = _merge_closed_agent_mr_feedback(
                closed_mr_feedback,
                _closed_agent_mr_feedback(gitlab),
            )
        except Exception as exc:
            closed_mrs_warning = f"could not read closed Agent MR feedback: {_safe_error(exc)}"
        now = _now()
        state.update(
            {
                "last_watch_run": now,
                "last_default_branch_head": head,
                "open_agent_mrs": open_mrs,
                "open_agent_mrs_details": open_mr_details,
                "closed_agent_mr_feedback": closed_mr_feedback,
            }
        )
        self.state_store.write(state)
        return self._write_report(
            self._report(
                "passed",
                "watch_completed",
                state_warning=warning,
                default_branch_head=head,
                open_agent_mrs_count=open_mrs,
                open_agent_mrs=open_mr_details,
                open_agent_mrs_warning=open_mrs_warning,
                closed_agent_mr_feedback=closed_mr_feedback,
                closed_agent_mr_feedback_count=len(closed_mr_feedback),
                closed_agent_mr_feedback_warning=closed_mrs_warning,
            )
        )

    def plan(
        self,
        *,
        focus: str | None = None,
        prefer_task_types: list[str] | None = None,
        prefer_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.config.schedule.enabled:
            return self._write_report(self._report("skipped", "schedule_disabled"))
        if not self.config.schedule.plan.enabled:
            return self._write_report(self._report("skipped", "plan_disabled"))
        state, warning = self.state_store.read()
        try:
            gitlab = self._gitlab()
            head = gitlab.get_default_branch_head()
        except Exception as exc:
            return self._write_report(self._report("failed", "gitlab_unavailable", error=str(exc), state_warning=warning))
        if (
            self.config.schedule.behavior.skip_if_default_branch_unchanged_since_last_plan
            and state.get("last_default_branch_head") == head
            and state.get("last_plan_run")
        ):
            return self._write_report(self._report("skipped", "default_branch_unchanged", default_branch_head=head))
        closed_feedback = list(state.get("closed_agent_mr_feedback") or [])
        preferred_types = _normalize_preferred_types(prefer_task_types)
        preferred_ids = _normalize_preferred_ids(prefer_task_ids)
        if hasattr(self.orchestrator, "plan_with_hints"):
            plan = self.orchestrator.plan_with_hints(
                focus=focus,
                preferred_task_types=preferred_types,
                preferred_task_ids=preferred_ids,
                closed_agent_mr_feedback=closed_feedback,
            )
        else:
            plan = self.orchestrator.plan()
        approved_plan, auto_report = AutoApprovalPolicy(self.config).apply(plan)
        self.artifacts.write_json("auto_approval_report", auto_report)
        self.artifacts.write_json("approved_plan", approved_plan)
        now = _now()
        selected_task_id = auto_report.get("selected_task_id")
        state.update(
            {
                "last_plan_run": now,
                "last_plan_run_id": self.run_id,
                "last_default_branch_head": head,
                "last_selected_task_id": selected_task_id,
            }
        )
        self.state_store.write(state)
        return self._write_report(self._report("passed", "plan_completed", state_warning=warning, selected_task_id=selected_task_id))

    def action(
        self,
        task_id: str | None = None,
        *,
        prefer_task_types: list[str] | None = None,
        prefer_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.config.schedule.enabled:
            return self._write_report(self._report("skipped", "schedule_disabled"))
        if not self.config.schedule.action.enabled:
            return self._write_report(self._report("skipped", "action_disabled"))
        if self.config.direct_main_push_enabled or self.config.auto_merge_enabled:
            return self._write_report(self._report("failed", "unsafe_scheduler_flags"))
        if not self.config.auto_approve.enabled:
            return self._write_report(self._report("skipped", "auto_approve_disabled"))
        if not self.config.push_agent_branches_enabled:
            return self._write_report(self._report("skipped", "branch_push_disabled"))

        state, warning = self.state_store.read()
        today = datetime.now(UTC).date().isoformat()
        if state.get("new_mrs_date") != today:
            state["new_mrs_today"] = 0
            state["new_mrs_date"] = today
        try:
            gitlab = self._gitlab()
            open_mrs = len(gitlab.list_open_agent_mrs())
        except Exception as exc:
            return self._write_report(self._report("failed", "gitlab_unavailable", error=str(exc), state_warning=warning))
        state["open_agent_mrs"] = open_mrs
        limits = self.config.schedule.limits
        if self.config.schedule.behavior.skip_if_open_agent_mr_exists and open_mrs > 0:
            self.state_store.write(state)
            return self._write_report(self._report("skipped", "open_agent_mr_exists", open_agent_mrs=open_mrs))
        if open_mrs >= limits.max_open_agent_mrs:
            self.state_store.write(state)
            return self._write_report(self._report("skipped", "open_agent_mr_limit_reached", open_agent_mrs=open_mrs, max_open_agent_mrs=limits.max_open_agent_mrs))
        if int(state.get("new_mrs_today") or 0) >= limits.max_new_mrs_per_day:
            self.state_store.write(state)
            return self._write_report(self._report("skipped", "daily_mr_limit_reached", new_mrs_today=state["new_mrs_today"], max_new_mrs_per_day=limits.max_new_mrs_per_day))
        cooldown_until = _parse_time(state.get("cooldown_until"))
        if cooldown_until and datetime.now(UTC) < cooldown_until:
            return self._write_report(self._report("skipped", "action_cooldown_active", cooldown_until=state.get("cooldown_until")))

        preferred_task_types = (
            _normalize_preferred_types(prefer_task_types)
            if prefer_task_types is not None
            else list(self.config.schedule.action.preferred_task_types)
        )
        preferred_task_ids = (
            _normalize_preferred_ids(prefer_task_ids)
            if prefer_task_ids is not None
            else list(self.config.schedule.action.preferred_task_ids)
        )
        approved_plan = None
        approved_plan_source = None
        if task_id is not None:
            try:
                approved_plan, approved_plan_source = self._load_current_approved_plan(state)
            except Exception as exc:
                return self._write_report(
                    self._report(
                        "failed",
                        "approved_plan_unavailable",
                        selected_task_id=task_id,
                        task_selection_reason="requested_task_id",
                        error=str(exc),
                        state_warning=warning,
                    )
                )

        result = self.orchestrator.full_flow(
            task_id=task_id,
            approved_plan=approved_plan,
            preferred_task_ids=[] if task_id is not None else preferred_task_ids,
            preferred_task_types=[] if task_id is not None else preferred_task_types,
            closed_agent_mr_feedback=list(state.get("closed_agent_mr_feedback") or []),
        )
        if result.get("status") == "blocked" and result.get("reason") == "no approved task available for implementation":
            return self._write_report(self._report("skipped", "no_auto_approved_task", full_flow=result))
        if task_id is not None and result.get("status") == "blocked" and result.get("reason") in {
            "selected task not found in approved plan",
            "selected task is not approved",
        }:
            return self._write_report(
                self._report(
                    "failed",
                    str(result.get("reason")),
                    selected_task_id=task_id,
                    task_selection_reason=result.get("task_selection_reason") or "requested_task_id",
                    approved_plan_source=approved_plan_source,
                    full_flow=result,
                    state_warning=warning,
                )
            )
        now_dt = datetime.now(UTC)
        state["last_action_run"] = now_dt.isoformat()
        state["cooldown_until"] = (now_dt + timedelta(hours=limits.min_hours_between_action_runs)).isoformat()
        if _mr_created(result):
            state["new_mrs_today"] = int(state.get("new_mrs_today") or 0) + 1
            state["new_mrs_date"] = today
        self.state_store.write(state)
        status = "passed" if result.get("status") in {"passed", "blocked"} else "failed"
        return self._write_report(
            self._report(
                status,
                "action_completed",
                state_warning=warning,
                selected_task_id=result.get("selected_task_id") or task_id,
                task_selection_reason=result.get("task_selection_reason"),
                task_selection_feedback_matches=result.get("task_selection_feedback_matches"),
                approved_plan_source=approved_plan_source,
                full_flow=result,
                new_mrs_today=state["new_mrs_today"],
            )
        )

    def review_comments(self) -> dict[str, Any]:
        if not self.config.schedule.enabled:
            return self._write_review_report(self._report("skipped", "schedule_disabled"))
        review_config = self.config.schedule.review_comments
        if not review_config.enabled:
            return self._write_review_report(self._report("skipped", "review_comments_disabled"))

        state, warning = self.state_store.read()
        try:
            gitlab = self._gitlab()
            current_user = self._safe_current_user(gitlab)
            mrs = gitlab.list_open_agent_merge_requests()
        except Exception as exc:
            return self._write_review_report(self._report("failed", "gitlab_unavailable", error=str(exc), state_warning=warning))

        processed_or_seen_processed = False
        initialized_seen = False
        state_changed = False
        for mr in mrs:
            if not is_agent_generated_mr(mr, default_branch=self.config.default_branch):
                continue
            mr_info = normalize_mr(mr)
            mr_iid = int(mr_info["iid"])
            stopped_key = mr_key(self.config.project_id or "", mr_iid)
            try:
                comments = flatten_merge_request_comments(
                    gitlab.list_merge_request_notes(mr_iid),
                    gitlab.list_merge_request_discussions(mr_iid),
                )
            except Exception as exc:
                return self._write_review_report(
                    self._report("failed", "gitlab_unavailable", error=f"could not read MR comments: {exc}", mr_iid=mr_iid, state_warning=warning)
                )

            if self._should_initialize_review_comment_seen(state, stopped_key):
                self._initialize_review_comment_seen(state, stopped_key, comments)
                initialized_seen = True
                state_changed = True
                continue

            last_seen_note_id = self._last_seen_note_id(state, stopped_key)
            for comment in comments:
                if comment.get("system"):
                    continue
                note_id = comment["id"]
                note_id_value = _note_id_value(note_id)
                if not review_config.process_history:
                    if note_id_value is not None and note_id_value <= last_seen_note_id:
                        processed_or_seen_processed = True
                        continue
                    if note_id_value is not None:
                        self._mark_review_comment_seen(state, stopped_key, note_id_value)
                        last_seen_note_id = max(last_seen_note_id, note_id_value)
                        state_changed = True
                key = review_comment_key(self.config.project_id or "", mr_iid, note_id)
                parsed = parse_review_command(comment.get("body", ""), allowed_commands=review_config.allowed_commands)
                if parsed is None:
                    continue
                if key in state.get("processed_review_comments", {}):
                    processed_or_seen_processed = True
                    continue
                self.artifacts.write_json("parsed_command", parsed.to_dict())

                if is_bot_author(comment.get("author", {}), current_user):
                    continue

                if not parsed.allowed:
                    response = self._command_not_allowed_response(parsed.command)
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="skipped",
                        reason="command_not_allowed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if not self._author_is_allowed(gitlab, comment.get("author", {})):
                    response = "AgentLab ignored this command because the author is not authorized."
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="skipped",
                        reason="unauthorized_comment",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                revision_command = _is_revision_command(parsed.command, parsed.propose_only)
                if revision_command and stopped_key in state.get("stopped_mrs", {}):
                    response = "AgentLab ignored this revision command because this MR is stopped. Use `/agent resume` first."
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="skipped",
                        reason="mr_stopped",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if revision_command and self._review_cooldown_active(state, stopped_key):
                    cooldown_until = state.get("review_comment_cooldowns", {}).get(stopped_key)
                    response = f"AgentLab skipped this revision command because the MR review-comment cooldown is active until `{cooldown_until}`."
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="skipped",
                        reason="review_comment_cooldown_active",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if parsed.command == "stop":
                    stopped = dict(state.get("stopped_mrs", {}))
                    stopped[stopped_key] = {
                        "stopped_at": _now(),
                        "author": author_username(comment.get("author", {})),
                        "reason": parsed.feedback,
                    }
                    state["stopped_mrs"] = stopped
                    response = "AgentLab will stop processing revision commands for this MR until `/agent resume` is used by an authorized user."
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="passed",
                        reason="comment_processed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if parsed.command == "resume":
                    stopped = dict(state.get("stopped_mrs", {}))
                    stopped.pop(stopped_key, None)
                    state["stopped_mrs"] = stopped
                    response = "AgentLab resumed processing revision commands for this MR."
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="passed",
                        reason="comment_processed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if parsed.command == "status":
                    response = self._status_response(mr_info)
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="passed",
                        reason="comment_processed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if parsed.command == "merge-status":
                    response = self._merge_status_response(mr_info)
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="passed",
                        reason="comment_processed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                if parsed.command == "explain":
                    response = self._explain_response(mr_info)
                    return self._post_and_finish(
                        gitlab,
                        state,
                        key,
                        comment,
                        parsed.command,
                        response,
                        status="passed",
                        reason="comment_processed",
                        mr_iid=mr_iid,
                        note_id=note_id,
                        source_branch=mr_info["source_branch"],
                        state_warning=warning,
                    )

                revision = self._run_revision(
                    gitlab,
                    mr_info,
                    parsed.command,
                    parsed.feedback,
                    note_id,
                    propose_only=parsed.propose_only,
                )
                response = self._revision_response(parsed.command, revision)
                status = "passed" if revision.get("status") == "passed" else "failed"
                reason = str(revision.get("reason") or "revision_failed")
                self._set_review_cooldown(state, stopped_key)
                return self._post_and_finish(
                    gitlab,
                    state,
                    key,
                    comment,
                    parsed.command,
                    response,
                    status=status,
                    reason=reason,
                    mr_iid=mr_iid,
                    note_id=note_id,
                    source_branch=mr_info["source_branch"],
                    commit_sha=revision.get("commit_sha"),
                    changed_files=revision.get("changed_files") or [],
                    state_warning=warning,
                )

        if state_changed:
            self.state_store.write(state)
        if initialized_seen:
            reason = "review_comments_initialized"
        else:
            reason = "already_processed" if processed_or_seen_processed else "no_agent_comment"
        return self._write_review_report(self._report("skipped", reason, state_warning=warning))

    def _gitlab(self) -> GitLabTool:
        return GitLabTool(self.config)

    def _load_current_approved_plan(self, state: dict[str, Any]) -> tuple[TaskPlan, str]:
        workspace_root = Path(self.config.workspace_root)
        run_id = state.get("last_plan_run_id")
        if isinstance(run_id, str) and run_id:
            path = workspace_root / run_id / "artifacts" / "approved_plan.json"
            if not path.exists():
                raise FileNotFoundError(f"approved_plan.json not found for last_plan_run_id {run_id}: {path}")
            return _load_approved_plan(path), str(path)
        path = _latest_approved_plan_path(workspace_root)
        if path is None:
            raise FileNotFoundError("approved_plan.json not found; run scheduler-plan before scheduler-action --task-id")
        return _load_approved_plan(path), str(path)

    @property
    def orchestrator(self) -> Orchestrator:
        if self._orchestrator is None:
            self._orchestrator = Orchestrator(self.config, run_id=self.run_id)
            self.artifacts = self._orchestrator.artifacts
        return self._orchestrator

    def _write_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self.artifacts.write_json("scheduler_report", report)
        return {"run_id": self.run_id, **report}

    def _write_review_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self.artifacts.write_json("review_comment_report", {"run_id": self.run_id, **report})
        return {"run_id": self.run_id, **report}

    def _report(self, status: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "schedule_enabled": self.config.schedule.enabled,
            "timestamp": _now(),
            **extra,
        }

    def _review_result(self, status: str, reason: str, **extra: Any) -> dict[str, Any]:
        return self._report(status, reason, **extra)

    def _post_and_finish(
        self,
        gitlab: Any,
        state: dict[str, Any],
        key: str,
        comment: dict[str, Any],
        command: str,
        response: str,
        *,
        status: str,
        reason: str,
        mr_iid: int,
        note_id: int | str,
        source_branch: str,
        commit_sha: Any = None,
        changed_files: list[str] | None = None,
        state_warning: str | None = None,
    ) -> dict[str, Any]:
        try:
            posted = gitlab.post_merge_request_note(mr_iid, response)
            self.artifacts.write_json("posted_response", {"mr_iid": mr_iid, "note_id": note_id, "body": response, "response": posted})
        except Exception as exc:
            status = "failed"
            reason = "response_post_failed"
            self.artifacts.write_json("posted_response", {"mr_iid": mr_iid, "note_id": note_id, "body": response, "error": str(exc)})
        self._mark_comment(state, key, comment, command, status)
        state["last_review_comment_run"] = _now()
        self.state_store.write(state)
        return self._write_review_report(
            self._review_result(
                status,
                reason,
                mr_iid=mr_iid,
                note_id=note_id,
                command=command,
                source_branch=source_branch,
                commit_sha=commit_sha,
                changed_files=changed_files or [],
                state_warning=state_warning,
            )
        )

    def _mark_comment(self, state: dict[str, Any], key: str, comment: dict[str, Any], command: str, status: str) -> None:
        processed = dict(state.get("processed_review_comments", {}))
        processed[key] = {
            "processed_at": _now(),
            "author": author_username(comment.get("author", {})),
            "command": command,
            "status": status,
            "run_id": self.run_id,
        }
        state["processed_review_comments"] = processed

    def _safe_current_user(self, gitlab: Any) -> Any | None:
        if not hasattr(gitlab, "get_current_user"):
            return None
        try:
            return gitlab.get_current_user()
        except Exception:
            return None

    def _author_is_allowed(self, gitlab: Any, author: Any) -> bool:
        review_config = self.config.schedule.review_comments
        username = (author_username(author) or "").lower()
        if username and username in {item.lower() for item in review_config.allowed_authors}:
            return True
        if not review_config.require_author_role:
            return False
        if hasattr(gitlab, "author_is_allowed"):
            try:
                return bool(
                    gitlab.author_is_allowed(
                        author,
                        allowed_authors=review_config.allowed_authors,
                        require_author_role=review_config.require_author_role,
                    )
                )
            except Exception:
                return False
        user_id = author_id(author)
        if user_id is not None and hasattr(gitlab, "get_project_member_role"):
            try:
                role = gitlab.get_project_member_role(user_id).get("role")
            except Exception:
                return False
            return str(role).lower() in {item.lower() for item in review_config.require_author_role}
        return False

    def _review_cooldown_active(self, state: dict[str, Any], key: str) -> bool:
        cooldown_until = _parse_time(state.get("review_comment_cooldowns", {}).get(key))
        return bool(cooldown_until and datetime.now(UTC) < cooldown_until)

    def _set_review_cooldown(self, state: dict[str, Any], key: str) -> None:
        minutes = self.config.schedule.review_comments.cooldown_minutes
        if minutes <= 0:
            return
        cooldowns = dict(state.get("review_comment_cooldowns", {}))
        cooldowns[key] = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
        state["review_comment_cooldowns"] = cooldowns

    def _should_initialize_review_comment_seen(self, state: dict[str, Any], key: str) -> bool:
        if self.config.schedule.review_comments.process_history:
            return False
        if state.get("processed_review_comments"):
            return False
        seen = state.get("review_comments_seen")
        return not isinstance(seen, dict) or key not in seen

    def _initialize_review_comment_seen(self, state: dict[str, Any], key: str, comments: list[dict[str, Any]]) -> None:
        last_seen = max((_note_id_value(comment.get("id")) or 0 for comment in comments), default=0)
        self._mark_review_comment_seen(state, key, last_seen, initialized=True)

    def _last_seen_note_id(self, state: dict[str, Any], key: str) -> int:
        seen = state.get("review_comments_seen")
        if not isinstance(seen, dict):
            return 0
        item = seen.get(key)
        if not isinstance(item, dict):
            return 0
        try:
            return int(item.get("last_seen_note_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _mark_review_comment_seen(self, state: dict[str, Any], key: str, note_id: int, *, initialized: bool = False) -> None:
        seen = dict(state.get("review_comments_seen") or {})
        previous = seen.get(key) if isinstance(seen.get(key), dict) else {}
        previous_last = _note_id_value(previous.get("last_seen_note_id")) if isinstance(previous, dict) else None
        seen[key] = {
            "initialized_at": previous.get("initialized_at") if isinstance(previous, dict) and previous.get("initialized_at") else _now(),
            "last_seen_note_id": max(previous_last or 0, note_id),
        }
        if initialized:
            seen[key]["initialized_at"] = _now()
        state["review_comments_seen"] = seen

    def _run_revision(
        self,
        gitlab: Any,
        mr_info: dict[str, Any],
        command: str,
        feedback: str,
        note_id: int | str,
        *,
        propose_only: bool = False,
    ) -> dict[str, Any]:
        mr_iid = int(mr_info["iid"])
        try:
            changed_files = self._mr_changed_files(gitlab, mr_iid)
            result = self.orchestrator.revise_existing_mr(
                mr_iid=mr_iid,
                source_branch=str(mr_info["source_branch"]),
                command=command,
                feedback=feedback,
                note_id=note_id,
                changed_files=changed_files,
                propose_only=propose_only,
            )
            return result
        except Exception as exc:
            return {
                "run_id": self.run_id,
                "status": "failed",
                "reason": "revision_failed",
                "source_branch": mr_info["source_branch"],
                "command": command,
                "propose_only": propose_only,
                "error": str(exc),
            }

    def _mr_changed_files(self, gitlab: Any, mr_iid: int) -> list[str] | None:
        if not hasattr(gitlab, "get_merge_request_changes"):
            return None
        try:
            return gitlab.get_merge_request_changes(mr_iid)
        except Exception:
            return None

    def _status_response(self, mr_info: dict[str, Any]) -> str:
        latest_gate = self._latest_artifact("gate_decision.json")
        latest_policy = self._latest_artifact("auto_approval_report.json")
        changed_files = self._artifact_changed_files() or []
        blockers = latest_gate.get("blockers") if isinstance(latest_gate, dict) else None
        policy_status = _policy_status(latest_policy)
        return (
            "AgentLab status for this MR.\n\n"
            f"- Run: `{self.run_id}`\n"
            f"- Branch: `{mr_info['source_branch']}`\n"
            f"- Changed files: `{', '.join(changed_files) if changed_files else 'unknown'}`\n"
            f"- Gatekeeper: `{_gate_status(latest_gate)}`\n"
            f"- Known blockers: `{', '.join(blockers) if blockers else 'None'}`\n"
            f"- Last policy status: `{policy_status}`"
        )

    def _merge_status_response(self, mr_info: dict[str, Any]) -> str:
        source_branch = str(mr_info["source_branch"])
        artifacts = self._latest_artifacts_for_branch(
            source_branch,
            [
                "gate_decision.json",
                "functional_test_report.json",
                "quality_review.json",
                "security_architecture_review.json",
            ],
        )
        if not any(artifacts.values()):
            return (
                "AgentLab merge status for this MR is unavailable.\n\n"
                f"- Branch: `{source_branch}`\n"
                "- No AgentLab gate report artifacts were found for this MR branch.\n"
                "- Recommendation: Do not merge yet"
            )

        gate = artifacts.get("gate_decision.json")
        functional = artifacts.get("functional_test_report.json")
        quality = artifacts.get("quality_review.json")
        security = artifacts.get("security_architecture_review.json")
        blockers = gate.get("blockers") if isinstance(gate, dict) and isinstance(gate.get("blockers"), list) else []
        merge_safe = _gate_allows_merge(gate)
        recommendation = "Manual merge is safe" if merge_safe else "Do not merge yet"
        return (
            "AgentLab merge status for this MR.\n\n"
            f"- Branch: `{source_branch}`\n"
            f"- Gate verdict: `{_gate_status(gate)}`\n"
            f"- Blockers: `{', '.join(str(item) for item in blockers) if blockers else 'None'}`\n"
            f"- Functional tests: `{_report_status(functional)}`\n"
            f"- Quality review: `{_review_status(quality)}`\n"
            f"- Security review: `{_review_status(security)}`\n"
            f"- Auto-merge: `{'enabled' if self.config.auto_merge_enabled else 'disabled'}`\n"
            f"- Recommendation: {recommendation}"
        )

    def _explain_response(self, mr_info: dict[str, Any]) -> str:
        latest_gate = self._latest_artifact("gate_decision.json")
        latest_policy = self._latest_artifact("auto_approval_report.json")
        changed_files = self._artifact_changed_files() or []
        return (
            "AgentLab explanation for this MR.\n\n"
            f"- Why: `{mr_info.get('title') or 'AgentLab generated this MR from an approved task.'}`\n"
            f"- Changed files: `{', '.join(changed_files) if changed_files else 'unknown'}`\n"
            f"- Relevant policy: `{_policy_status(latest_policy)}`\n"
            f"- Gatekeeper: `{_gate_status(latest_gate)}`\n"
            "- Tests/reviews: `See the latest functional_test_report, build_security_report, quality_review, and security_architecture_review artifacts when present.`"
        )

    def _latest_artifact(self, artifact_name: str) -> dict[str, Any] | None:
        root = Path(self.config.workspace_root)
        candidates = sorted(root.glob(f"*/artifacts/{artifact_name}"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        return None

    def _latest_artifacts_for_branch(self, source_branch: str, artifact_names: list[str]) -> dict[str, dict[str, Any] | None]:
        root = Path(self.config.workspace_root)
        artifact_dirs = sorted(
            {path.parent for name in artifact_names for path in root.glob(f"*/artifacts/{name}") if path.is_file()},
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for artifacts_dir in artifact_dirs:
            if not _artifacts_match_source_branch(artifacts_dir, source_branch):
                continue
            return {name: _read_artifact_json(artifacts_dir / name) for name in artifact_names}
        return {name: None for name in artifact_names}

    def _artifact_changed_files(self) -> list[str] | None:
        for name in ("implementation_report.json", "diff_stats.json"):
            artifact = self._latest_artifact(name)
            if isinstance(artifact, dict):
                changed = artifact.get("changed_files")
                if isinstance(changed, list):
                    return [str(path) for path in changed]
        return None

    def _revision_response(self, command: str, revision: dict[str, Any]) -> str:
        if revision.get("reason") == "policy_blocked":
            auto = revision.get("auto_approval") if isinstance(revision.get("auto_approval"), dict) else {}
            return _policy_blocked_response(auto, revision, self.config)
        if revision.get("propose_only") or revision.get("reason") == "proposal_generated":
            return _proposal_response(command, revision, self.run_id)
        if revision.get("status") != "passed":
            details = []
            if revision.get("proposal_run_id"):
                details.append(f"Proposal run: `{revision.get('proposal_run_id')}`")
            if revision.get("stale_reason"):
                details.append(f"Stale check: {revision.get('stale_reason')}")
            if revision.get("error"):
                details.append(f"Error: {revision.get('error')}")
            return (
                "AgentLab could not apply this request.\n\n"
                f"Reason: {revision.get('reason') or 'revision_failed'}"
                + (("\n" + "\n".join(details)) if details else "")
            )

        gate = revision.get("gate") if isinstance(revision.get("gate"), dict) else {}
        changed = revision.get("changed_files") or []
        blockers = gate.get("blockers") or []
        policy = revision.get("auto_approval") if isinstance(revision.get("auto_approval"), dict) else {}
        return (
            f"AgentLab processed `/agent {command}`.\n\n"
            f"Run: {revision.get('run_id') or self.run_id}\n"
            f"Proposal run: {revision.get('proposal_run_id') or '<none>'}\n"
            f"Commit: {revision.get('commit_sha') or '<none>'}\n"
            "Changed files:\n"
            f"{_bullet_list(changed)}\n\n"
            "Policy:\n"
            f"- {_policy_status(policy)}\n"
            f"- risk_score: {_policy_risk_score(policy)}\n\n"
            "Gate:\n"
            f"- {_gate_status(gate)}\n"
            f"- blockers: {', '.join(blockers) if blockers else 'None'}"
        )

    @staticmethod
    def _command_not_allowed_response(command: str) -> str:
        return (
            "AgentLab rejected this command because it is not allowed.\n\n"
            f"Command: `/agent {command}`"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _note_id_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_approved_plan(path: Path) -> TaskPlan:
    return TaskPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _open_agent_mr_details(mrs: list[Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for mr in mrs:
        normalized = normalize_mr(mr)
        details.append(
            {
                "iid": normalized.get("iid"),
                "title": str(normalized.get("title") or ""),
                "source_branch": str(normalized.get("source_branch") or ""),
                "web_url": normalized.get("web_url"),
                "labels": [str(label) for label in normalized.get("labels", [])],
                "updated_at": normalized.get("updated_at"),
            }
        )
    return details


def _closed_agent_mr_feedback(gitlab: Any) -> list[dict[str, Any]]:
    if hasattr(gitlab, "list_closed_agent_merge_requests"):
        mrs = gitlab.list_closed_agent_merge_requests()
    elif hasattr(gitlab, "list_agent_merge_requests"):
        mrs = gitlab.list_agent_merge_requests(state="closed", label="agent/generated")
    else:
        return []
    feedback: list[dict[str, Any]] = []
    for mr in mrs:
        normalized = normalize_mr(mr)
        if not _is_unmerged_closed_agent_mr(normalized):
            continue
        iid = normalized.get("iid")
        try:
            mr_iid = int(iid)
        except (TypeError, ValueError):
            continue
        changed_files = _safe_mr_changed_files(gitlab, mr_iid)
        comments = _safe_mr_comments(gitlab, mr_iid)
        feedback.append(
            {
                "iid": mr_iid,
                "title": str(normalized.get("title") or ""),
                "source_branch": str(normalized.get("source_branch") or ""),
                "changed_files": changed_files,
                "labels": [str(label) for label in normalized.get("labels", [])],
                "closed_at": normalized.get("closed_at") or normalized.get("updated_at"),
                "reason": _stop_reason_from_comments(comments),
            }
        )
    return feedback


def _is_unmerged_closed_agent_mr(mr: dict[str, Any]) -> bool:
    labels = {str(label) for label in mr.get("labels", [])}
    return (
        str(mr.get("state") or "").lower() == "closed"
        and str(mr.get("source_branch") or "").startswith("agent/")
        and "agent/generated" in labels
        and not mr.get("merged_at")
    )


def _safe_mr_changed_files(gitlab: Any, mr_iid: int) -> list[str]:
    if not hasattr(gitlab, "get_merge_request_changes"):
        return []
    try:
        return [str(path) for path in (gitlab.get_merge_request_changes(mr_iid) or [])]
    except Exception:
        return []


def _safe_mr_comments(gitlab: Any, mr_iid: int) -> list[dict[str, Any]]:
    try:
        return flatten_merge_request_comments(
            gitlab.list_merge_request_notes(mr_iid) if hasattr(gitlab, "list_merge_request_notes") else [],
            gitlab.list_merge_request_discussions(mr_iid) if hasattr(gitlab, "list_merge_request_discussions") else [],
        )
    except Exception:
        return []


def _stop_reason_from_comments(comments: list[dict[str, Any]]) -> str | None:
    for comment in reversed(comments):
        body = str(comment.get("body") or "")
        inline_match = STOP_REASON_INLINE_RE.search(body)
        if inline_match:
            reason = inline_match.group("reason").strip()
            return reason or None
        parsed = parse_review_command(body)
        if parsed is None or parsed.command != "stop" or not parsed.allowed:
            continue
        match = STOP_REASON_RE.match(parsed.feedback)
        if match:
            reason = match.group("reason").strip()
            return reason or None
    return None


def _merge_closed_agent_mr_feedback(existing: list[Any], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_iid: dict[int, dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        try:
            iid = int(item.get("iid"))
        except (TypeError, ValueError):
            continue
        by_iid[iid] = dict(item)
    for item in incoming:
        by_iid[int(item["iid"])] = item
    return sorted(by_iid.values(), key=lambda item: str(item.get("closed_at") or ""), reverse=True)[:50]


def _safe_error(exc: Exception) -> str:
    return str(redact_secrets(str(exc)))


def _normalize_preferred_ids(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        stripped = str(value).strip()
        if stripped and stripped not in normalized:
            normalized.append(stripped)
    return normalized


def _normalize_preferred_types(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        stripped = str(value).strip().lower()
        if stripped and stripped not in normalized:
            normalized.append(stripped)
    return normalized


def _latest_approved_plan_path(workspace_root: Path) -> Path | None:
    candidates = [path for path in workspace_root.glob("*/artifacts/approved_plan.json") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _mr_created(result: dict[str, Any]) -> bool:
    mr = result.get("merge_request")
    return isinstance(mr, dict) and mr.get("status") == "created"


def _is_revision_command(command: str, propose_only: bool = False) -> bool:
    return propose_only or command in {"revise", "fix", "apply"}


def _gate_status(gate: Any) -> str:
    if not isinstance(gate, dict):
        return "unknown"
    if gate.get("verdict"):
        return str(gate["verdict"])
    if gate.get("allowed") is True:
        return "passed"
    if gate.get("allowed") is False:
        return "blocked"
    return "unknown"


def _gate_allows_merge(gate: Any) -> bool:
    return isinstance(gate, dict) and gate.get("allowed") is True and not gate.get("blockers")


def _report_status(report: Any) -> str:
    if not isinstance(report, dict):
        return "unknown"
    status = report.get("status")
    if status:
        return str(status)
    if report.get("passed") is True:
        return "passed"
    if report.get("passed") is False:
        return "failed"
    return "unknown"


def _review_status(report: Any) -> str:
    if not isinstance(report, dict):
        return "unknown"
    verdict = report.get("verdict")
    if verdict:
        return str(verdict)
    status = report.get("status")
    return str(status) if status else "unknown"


def _read_artifact_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _artifacts_match_source_branch(artifacts_dir: Path, source_branch: str) -> bool:
    implementation = _read_artifact_json(artifacts_dir / "implementation_report.json")
    if isinstance(implementation, dict) and implementation.get("branch") == source_branch:
        return True
    finalization = _read_artifact_json(artifacts_dir / "mr_finalization_result.json")
    mr = finalization.get("mr") if isinstance(finalization, dict) else None
    if isinstance(mr, dict) and mr.get("source_branch") == source_branch:
        return True
    return False


def _proposal_response(command: str, revision: dict[str, Any], run_id: str) -> str:
    if revision.get("status") != "passed":
        return (
            "AgentLab could not generate a proposed revision.\n\n"
            f"Reason: {revision.get('reason') or 'proposal_failed'}\n"
            "Commit: none\n"
            "Push: skipped"
        )

    changed = revision.get("changed_files") or []
    artifacts = _proposal_artifacts(revision.get("proposal_artifacts") or revision.get("patch_artifacts") or [])
    validation = revision.get("proposal_validation") if isinstance(revision.get("proposal_validation"), dict) else {}
    validation_status = str(validation.get("status") or "unknown")
    validation_blockers = validation.get("blockers") if isinstance(validation.get("blockers"), list) else []
    policy = revision.get("auto_approval") if isinstance(revision.get("auto_approval"), dict) else {}
    return (
        "AgentLab generated a proposed revision but did not push it.\n\n"
        f"Run: {revision.get('run_id') or run_id}\n"
        "Commit: none\n"
        "Push: skipped\n"
        "Changed files proposed:\n"
        f"{_bullet_list(changed)}\n\n"
        "Proposal validation:\n"
        f"- {validation_status}\n"
        f"- blockers: {', '.join(str(item) for item in validation_blockers) if validation_blockers else 'None'}\n\n"
        "Policy:\n"
        f"- {_policy_status(policy)}\n"
        f"- risk_score: {_policy_risk_score(policy)}\n\n"
        "Patch artifact:\n"
        f"{_bullet_list(artifacts)}"
    )


def _proposal_artifacts(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    wanted = ["structured_proposal.json", "proposed.diff", "structured_proposal_report.json"]
    present = {str(value) for value in values}
    return [name for name in wanted if name in present]


def _policy_status(policy: Any) -> str:
    if not isinstance(policy, dict):
        return "unknown"
    if policy.get("approved_tasks"):
        return "approved"
    rejected = policy.get("rejected_tasks")
    if rejected:
        return "blocked"
    if policy.get("enabled") is False:
        return "disabled"
    return "unknown"


def _policy_risk_score(policy: Any) -> Any:
    details = _first_auto_approval_details(policy if isinstance(policy, dict) else {})
    if "risk_score" in details:
        return details["risk_score"]
    evaluated = policy.get("evaluated_tasks") if isinstance(policy, dict) else None
    if isinstance(evaluated, list) and evaluated:
        item = evaluated[0]
        if isinstance(item, dict):
            item_details = item.get("details")
            if isinstance(item_details, dict):
                return item_details.get("risk_score", "unknown")
    return "unknown"


def _policy_blocked_response(policy: dict[str, Any], revision: dict[str, Any], config: AppConfig) -> str:
    item = _first_auto_approval_item(policy)
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    task = revision.get("task") if isinstance(revision.get("task"), dict) else {}
    policy_config = policy.get("policy_config") if isinstance(policy.get("policy_config"), dict) else {}
    reasons = _auto_approval_reasons(policy)
    affected_files = _value_or_default(details.get("affected_files"), task.get("affected_files"), revision.get("changed_files"), [])
    disallowed_paths = details.get("disallowed_paths") if isinstance(details.get("disallowed_paths"), list) else []
    blocked_matches = details.get("blocked_paths_matched") if isinstance(details.get("blocked_paths_matched"), list) else []

    sections = [
        "AgentLab could not apply this request.",
        "",
        "Reason: policy_blocked",
        "Policy reasons:",
        _bullet_list(reasons),
        "",
        "Task:",
        f"- task_type: {_value_or_default(details.get('task_type'), task.get('task_type'), 'unknown')}",
        f"- risk_score: {_value_or_default(details.get('risk_score'), task.get('risk_score'), 'unknown')}",
        "- affected_files:",
        _indented_bullet_list(affected_files),
        "",
        "AutoApproval:",
        f"- enabled: {str(_value_or_default(policy.get('enabled'), policy_config.get('enabled'), config.auto_approve.enabled)).lower()}",
        "- allowed_task_types:",
        _indented_bullet_list(_value_or_default(details.get("allowed_task_types"), policy_config.get("allowed_task_types"), config.auto_approve.allowed_task_types)),
        "- allowed_paths:",
        _indented_bullet_list(_value_or_default(details.get("allowed_paths"), policy_config.get("allowed_paths"), config.auto_approve.allowed_paths)),
        "- blocked_paths:",
        _indented_bullet_list(_value_or_default(policy_config.get("blocked_paths"), config.auto_approve.blocked_paths)),
    ]
    if disallowed_paths:
        sections.extend(["", "Path details:", "- disallowed_paths:", _indented_bullet_list(disallowed_paths)])
    if blocked_matches:
        sections.extend(["- blocked_paths_matched:", _indented_bullet_list(_format_blocked_matches(blocked_matches))])
    return "\n".join(sections)


def _first_auto_approval_item(policy: dict[str, Any]) -> dict[str, Any]:
    for key in ("rejected_tasks", "evaluated_tasks"):
        items = policy.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                if key == "rejected_tasks" or item.get("approved") is False:
                    return item
    return {}


def _first_auto_approval_details(policy: dict[str, Any]) -> dict[str, Any]:
    item = _first_auto_approval_item(policy)
    return item["details"] if isinstance(item.get("details"), dict) else {}


def _auto_approval_reasons(policy: dict[str, Any]) -> list[str]:
    item = _first_auto_approval_item(policy)
    reasons = item.get("reasons")
    if isinstance(reasons, list):
        return [str(reason) for reason in reasons if str(reason)]
    if policy.get("enabled") is False:
        return ["auto_approve_disabled"]
    return ["unknown_policy_rejection"]


def _value_or_default(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _format_blocked_matches(values: list[Any]) -> list[str]:
    formatted: list[str] = []
    for value in values:
        if isinstance(value, dict):
            path = value.get("path")
            pattern = value.get("pattern")
            formatted.append(f"{path} (matched {pattern})" if pattern else str(path))
        else:
            formatted.append(str(value))
    return formatted


def _bullet_list(values: Any) -> str:
    if not values:
        return "- None"
    if not isinstance(values, list):
        values = [values]
    return "\n".join(f"- {value}" for value in values)


def _indented_bullet_list(values: Any) -> str:
    if not values:
        return "  - <none>"
    if not isinstance(values, list):
        values = [values]
    return "\n".join(f"  - {value}" for value in values)


def reset_scheduler_state(config: AppConfig) -> dict[str, Any]:
    store = SchedulerStateStore(config.workspace_root)
    existed = store.path.exists()
    if existed:
        store.path.unlink()
    return {
        "status": "passed",
        "reason": "scheduler_state_removed",
        "path": str(store.path),
        "existed": existed,
    }


def scheduler_status(config: AppConfig) -> dict[str, Any]:
    store = SchedulerStateStore(config.workspace_root)
    state, warning = store.read()
    return {
        "status": "passed",
        "reason": "scheduler_state_read",
        "path": str(store.path),
        "exists": store.path.exists(),
        "state_warning": warning,
        "last_default_branch_head": state.get("last_default_branch_head"),
        "last_watch_run": state.get("last_watch_run"),
        "last_plan_run": state.get("last_plan_run"),
        "last_action_run": state.get("last_action_run"),
        "last_review_comment_run": state.get("last_review_comment_run"),
        "open_agent_mrs": state.get("open_agent_mrs"),
        "closed_agent_mr_feedback": len(state.get("closed_agent_mr_feedback") or []),
        "new_mrs_today": state.get("new_mrs_today"),
        "processed_review_comments": len(state.get("processed_review_comments") or {}),
        "stopped_mrs": len(state.get("stopped_mrs") or {}),
    }
