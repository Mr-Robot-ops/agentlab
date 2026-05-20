from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.orchestrator import Orchestrator
from agentlab.policies.auto_approval import AutoApprovalPolicy
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
}


class SchedulerStateStore:
    def __init__(self, workspace_root: str | Path) -> None:
        self.path = Path(workspace_root) / "scheduler" / "state.json"

    def read(self) -> tuple[dict[str, Any], str | None]:
        if not self.path.exists():
            return dict(STATE_DEFAULTS), None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            return dict(STATE_DEFAULTS), f"state_file_invalid: {exc}"
        state = {**STATE_DEFAULTS, **{key: raw.get(key) for key in STATE_DEFAULTS if key in raw}}
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

    def _report(self, status: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "schedule_enabled": self.config.schedule.enabled,
            "timestamp": _now(),
            **extra,
        }


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
        "open_agent_mrs": state.get("open_agent_mrs"),
        "new_mrs_today": state.get("new_mrs_today"),
    }
