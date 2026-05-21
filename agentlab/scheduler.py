from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from agentlab.tools.gitlab_tool import GitLabTool


STATE_DEFAULTS: dict[str, Any] = {
    "last_watch_run": None,
    "last_plan_run": None,
    "last_action_run": None,
    "last_default_branch_head": None,
    "new_mrs_today": 0,
    "new_mrs_date": None,
    "open_agent_mrs": 0,
    "cooldown_until": None,
    "last_selected_task_id": None,
    "last_review_comment_run": None,
    "processed_review_comments": {},
    "stopped_mrs": {},
    "review_comment_cooldowns": {},
}


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
            open_mrs = len(gitlab.list_open_agent_mrs())
        except Exception as exc:
            return self._write_report(self._report("failed", "gitlab_unavailable", error=str(exc), state_warning=warning))
        now = _now()
        state.update(
            {
                "last_watch_run": now,
                "last_default_branch_head": head,
                "open_agent_mrs": open_mrs,
            }
        )
        self.state_store.write(state)
        return self._write_report(self._report("passed", "watch_completed", state_warning=warning, default_branch_head=head, open_agent_mrs=open_mrs))

    def plan(self) -> dict[str, Any]:
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
        plan = self.orchestrator.plan()
        approved_plan, auto_report = AutoApprovalPolicy(self.config).apply(plan)
        self.artifacts.write_json("auto_approval_report", auto_report)
        self.artifacts.write_json("approved_plan", approved_plan)
        now = _now()
        selected_task_id = auto_report.get("selected_task_id")
        state.update({"last_plan_run": now, "last_default_branch_head": head, "last_selected_task_id": selected_task_id})
        self.state_store.write(state)
        return self._write_report(self._report("passed", "plan_completed", state_warning=warning, selected_task_id=selected_task_id))

    def action(self) -> dict[str, Any]:
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

        result = self.orchestrator.full_flow()
        if result.get("status") == "blocked" and result.get("reason") == "no approved task available for implementation":
            return self._write_report(self._report("skipped", "no_auto_approved_task", full_flow=result))
        now_dt = datetime.now(UTC)
        state["last_action_run"] = now_dt.isoformat()
        state["cooldown_until"] = (now_dt + timedelta(hours=limits.min_hours_between_action_runs)).isoformat()
        if _mr_created(result):
            state["new_mrs_today"] = int(state.get("new_mrs_today") or 0) + 1
            state["new_mrs_date"] = today
        self.state_store.write(state)
        status = "passed" if result.get("status") in {"passed", "blocked"} else "failed"
        return self._write_report(self._report(status, "action_completed", state_warning=warning, full_flow=result, new_mrs_today=state["new_mrs_today"]))

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
        for mr in mrs:
            if not is_agent_generated_mr(mr, default_branch=self.config.default_branch):
                continue
            mr_info = normalize_mr(mr)
            mr_iid = int(mr_info["iid"])
            try:
                comments = flatten_merge_request_comments(
                    gitlab.list_merge_request_notes(mr_iid),
                    gitlab.list_merge_request_discussions(mr_iid),
                )
            except Exception as exc:
                return self._write_review_report(
                    self._report("failed", "gitlab_unavailable", error=f"could not read MR comments: {exc}", mr_iid=mr_iid, state_warning=warning)
                )

            for comment in comments:
                if comment.get("system"):
                    continue
                note_id = comment["id"]
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

                stopped_key = mr_key(self.config.project_id or "", mr_iid)
                if parsed.command in {"revise", "fix"} and stopped_key in state.get("stopped_mrs", {}):
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

                if parsed.command in {"revise", "fix"} and self._review_cooldown_active(state, stopped_key):
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

                revision = self._run_revision(gitlab, mr_info, parsed.command, parsed.feedback, note_id)
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

        reason = "already_processed" if processed_or_seen_processed else "no_agent_comment"
        return self._write_review_report(self._report("skipped", reason, state_warning=warning))

    def _gitlab(self) -> GitLabTool:
        return GitLabTool(self.config)

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

    def _run_revision(
        self,
        gitlab: Any,
        mr_info: dict[str, Any],
        command: str,
        feedback: str,
        note_id: int | str,
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
            )
            return result
        except Exception as exc:
            return {
                "run_id": self.run_id,
                "status": "failed",
                "reason": "revision_failed",
                "source_branch": mr_info["source_branch"],
                "command": command,
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
            details = _first_auto_approval_details(auto)
            disallowed = details.get("disallowed_paths") or []
            allowed = details.get("allowed_paths") or self.config.auto_approve.allowed_paths
            return (
                "AgentLab could not apply this request.\n\n"
                "Reason: policy_blocked\n"
                "Disallowed paths:\n"
                f"{_bullet_list(disallowed)}\n\n"
                "Allowed paths:\n"
                f"{_bullet_list(allowed)}"
            )
        if revision.get("status") != "passed":
            return (
                "AgentLab could not apply this request.\n\n"
                f"Reason: {revision.get('reason') or 'revision_failed'}"
            )

        gate = revision.get("gate") if isinstance(revision.get("gate"), dict) else {}
        changed = revision.get("changed_files") or []
        blockers = gate.get("blockers") or []
        policy = revision.get("auto_approval") if isinstance(revision.get("auto_approval"), dict) else {}
        return (
            f"AgentLab processed `/agent {command}`.\n\n"
            f"Run: {revision.get('run_id') or self.run_id}\n"
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


def _mr_created(result: dict[str, Any]) -> bool:
    mr = result.get("merge_request")
    return isinstance(mr, dict) and mr.get("status") == "created"


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


def _first_auto_approval_details(policy: dict[str, Any]) -> dict[str, Any]:
    for key in ("rejected_tasks", "evaluated_tasks"):
        items = policy.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("details"), dict):
                return item["details"]
    return {}


def _bullet_list(values: Any) -> str:
    if not values:
        return "- None"
    if not isinstance(values, list):
        values = [values]
    return "\n".join(f"- {value}" for value in values)


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
        "new_mrs_today": state.get("new_mrs_today"),
        "processed_review_comments": len(state.get("processed_review_comments") or {}),
        "stopped_mrs": len(state.get("stopped_mrs") or {}),
    }
