from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace

from agentlab.config import AppConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.scheduler import Scheduler, SchedulerStateStore, reset_scheduler_state, scheduler_status


def config(tmp_path: Path, **overrides: object) -> AppConfig:
    values = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": tmp_path / "repo",
        "workspace_root": tmp_path / "runs",
        "push_agent_branches_enabled": True,
        "auto_approve": {"enabled": True},
        "schedule": {"enabled": True},
    }
    values.update(overrides)
    Path(values["target_repo_path"]).mkdir(parents=True, exist_ok=True)
    return AppConfig.model_validate(values)


class FakeGitLab:
    def __init__(
        self,
        *,
        head: str = "abc",
        open_mrs: int = 0,
        closed_mrs: list[object] | None = None,
        notes: dict[int, list[dict[str, object]]] | None = None,
        changes: dict[int, list[str]] | None = None,
        fail_open_mrs: bool = False,
        fail_closed_mrs: bool = False,
    ) -> None:
        self.head = head
        self.open_mrs = open_mrs
        self.closed_mrs = closed_mrs or []
        self.notes = notes or {}
        self.changes = changes or {}
        self.fail_open_mrs = fail_open_mrs
        self.fail_closed_mrs = fail_closed_mrs

    def get_default_branch_head(self) -> str:
        return self.head

    def list_open_agent_mrs(self) -> list[object]:
        if self.fail_open_mrs:
            raise RuntimeError("token=super-secret GitLab MR list failed")
        return [
            SimpleNamespace(
                id=index,
                mr_id=index,
                iid=index,
                title=f"Agent MR {index}",
                source_branch=f"agent/task-{index}",
                target_branch="main",
                web_url=f"https://gitlab.example.com/project/-/merge_requests/{index}",
                labels=["agent/generated"],
                updated_at=f"2026-05-22T0{index}:00:00Z",
            )
            for index in range(1, self.open_mrs + 1)
        ]

    def list_closed_agent_merge_requests(self) -> list[object]:
        if self.fail_closed_mrs:
            raise RuntimeError("token=super-secret closed MR list failed")
        return self.closed_mrs

    def list_merge_request_notes(self, mr_iid: int) -> list[dict[str, object]]:
        return self.notes.get(mr_iid, [])

    def list_merge_request_discussions(self, mr_iid: int) -> list[dict[str, object]]:
        return []

    def get_merge_request_changes(self, mr_iid: int) -> list[str]:
        return self.changes.get(mr_iid, [])


class FakeOrchestrator:
    def __init__(self, cfg: AppConfig, *, result: dict[str, object] | None = None) -> None:
        self.run_id = "run-1"
        from agentlab.artifacts import ArtifactStore

        self.artifacts = ArtifactStore(Path(cfg.workspace_root) / self.run_id, self.run_id)
        self.result = result or {"status": "passed", "merge_request": {"status": "created"}}
        self.plan_called = False
        self.full_flow_called = False
        self.full_flow_task_id = None
        self.full_flow_approved_plan = None
        self.full_flow_preferred_task_ids = None
        self.full_flow_preferred_task_types = None
        self.full_flow_closed_agent_mr_feedback = None

    def plan(self) -> TaskPlan:
        self.plan_called = True
        return TaskPlan(
            tasks=[
                AgentTask(
                    id="docs-readme",
                    title="Docs",
                    task_type=TaskType.DOCS,
                    risk_level=RiskLevel.LOW,
                    risk_score=1,
                    affected_files=["README.md"],
                    forbidden_actions=["Do not change code."],
                )
            ]
        )

    def full_flow(
        self,
        *,
        task_id=None,
        approved_plan=None,
        auto_approval_report=None,
        preferred_task_ids=None,
        preferred_task_types=None,
        closed_agent_mr_feedback=None,
    ) -> dict[str, object]:
        self.full_flow_called = True
        self.full_flow_task_id = task_id
        self.full_flow_approved_plan = approved_plan
        self.full_flow_preferred_task_ids = preferred_task_ids
        self.full_flow_preferred_task_types = preferred_task_types
        self.full_flow_closed_agent_mr_feedback = closed_agent_mr_feedback
        if task_id == "missing-task":
            return {
                "status": "blocked",
                "reason": "selected task not found in approved plan",
                "selected_task_id": task_id,
                "task_selection_reason": "requested_task_id",
            }
        if task_id == "rejected-task":
            return {
                "status": "blocked",
                "reason": "selected task is not approved",
                "selected_task_id": task_id,
                "task_selection_reason": "requested_task_id",
            }
        return {
            **self.result,
            "selected_task_id": task_id or self.result.get("selected_task_id"),
            "task_selection_reason": self.result.get("task_selection_reason", "auto_approval_default"),
        }


class HelperScheduler(Scheduler):
    def __init__(self, cfg: AppConfig, *, gitlab: FakeGitLab | None = None, result: dict[str, object] | None = None) -> None:
        self.config = cfg
        self.run_id = "run-1"
        self.run_dir = Path(cfg.workspace_root) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._orchestrator = FakeOrchestrator(cfg, result=result)
        self.state_store = SchedulerStateStore(cfg.workspace_root)
        self.artifacts = self.orchestrator.artifacts
        self.fake_gitlab = gitlab or FakeGitLab()

    def _gitlab(self) -> FakeGitLab:
        return self.fake_gitlab


def task(task_id: str, task_type: TaskType, *, approved: bool) -> AgentTask:
    affected_files = ["README.md"] if task_type == TaskType.DOCS else ["tests/smoke.py"]
    return AgentTask(
        id=task_id,
        title=task_id,
        task_type=task_type,
        risk_level=RiskLevel.LOW,
        risk_score=1,
        affected_files=affected_files,
        approved=approved,
    )


def write_approved_plan(scheduler: HelperScheduler, plan: TaskPlan) -> None:
    plan_run_id = "plan-run"
    artifacts_dir = Path(scheduler.config.workspace_root) / plan_run_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "approved_plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    state, _ = scheduler.state_store.read()
    state["last_plan_run_id"] = plan_run_id
    scheduler.state_store.write(state)


def test_schedule_defaults_disabled(tmp_path: Path) -> None:
    cfg = config(tmp_path, schedule={})

    assert cfg.schedule.enabled is False


def test_schedule_block_parsed(tmp_path: Path) -> None:
    cfg = config(tmp_path, schedule={"enabled": True, "watch": {"enabled": True, "cron": "*/5 * * * *"}})

    assert cfg.schedule.enabled is True
    assert cfg.schedule.watch.cron == "*/5 * * * *"


def test_schedule_action_preferred_tasks_parsed(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        schedule={
            "enabled": True,
            "action": {
                "preferred_task_types": [" Tests ", "docs", "tests"],
                "preferred_task_ids": [" tests-02-smoke-baseline ", "docs-01-credentials"],
            },
        },
    )

    assert cfg.schedule.action.preferred_task_types == ["tests", "docs"]
    assert cfg.schedule.action.preferred_task_ids == ["tests-02-smoke-baseline", "docs-01-credentials"]


def test_scheduler_state_missing_initializes_and_writes(tmp_path: Path) -> None:
    store = SchedulerStateStore(tmp_path)
    state, warning = store.read()

    assert warning is None
    assert state["new_mrs_today"] == 0
    state["last_default_branch_head"] = "abc"
    store.write(state)
    loaded, warning = store.read()
    assert warning is None
    assert loaded["last_default_branch_head"] == "abc"


def test_scheduler_state_broken_file_is_safe(tmp_path: Path) -> None:
    store = SchedulerStateStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")

    state, warning = store.read()

    assert warning and "state_file_invalid" in warning
    assert state["new_mrs_today"] == 0


def test_scheduler_watch_writes_report_and_updates_head(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="head1", open_mrs=1))

    report = scheduler.watch()

    assert report["status"] == "passed"
    assert report["default_branch_head"] == "head1"
    state, _ = scheduler.state_store.read()
    assert state["last_default_branch_head"] == "head1"
    assert state["open_agent_mrs"] == 1
    assert (scheduler.artifacts.artifacts_dir / "scheduler_report.json").exists()
    assert scheduler.orchestrator.full_flow_called is False


def test_scheduler_watch_reports_open_agent_mr_details(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="head1", open_mrs=1))

    report = scheduler.watch()

    assert report["open_agent_mrs_count"] == 1
    assert report["open_agent_mrs"] == [
        {
            "iid": 1,
            "title": "Agent MR 1",
            "source_branch": "agent/task-1",
            "web_url": "https://gitlab.example.com/project/-/merge_requests/1",
            "labels": ["agent/generated"],
            "updated_at": "2026-05-22T01:00:00Z",
        }
    ]
    scheduler_report = json.loads((scheduler.artifacts.artifacts_dir / "scheduler_report.json").read_text(encoding="utf-8"))
    assert scheduler_report["open_agent_mrs"][0]["source_branch"] == "agent/task-1"


def test_scheduler_watch_records_closed_agent_mr_feedback(tmp_path: Path) -> None:
    closed_mr = SimpleNamespace(
        id=18,
        mr_id=18,
        iid=18,
        title="Add smoke baseline",
        source_branch="agent/tests-02-smoke-baseline-run",
        target_branch="main",
        state="closed",
        web_url="https://gitlab.example.com/project/-/merge_requests/18",
        labels=["agent/generated"],
        updated_at="2026-05-22T11:59:00Z",
        closed_at="2026-05-22T12:00:00Z",
        merged_at=None,
    )
    scheduler = HelperScheduler(
        config(tmp_path),
        gitlab=FakeGitLab(
            head="head1",
            closed_mrs=[closed_mr],
            changes={18: ["tests/smoke.py"]},
            notes={
                18: [
                    {
                        "id": 1,
                        "body": "Closing manually.\n/agent stop reason: baseline was flaky",
                        "created_at": "2026-05-22T11:58:00Z",
                    }
                ]
            },
        ),
    )

    report = scheduler.watch()
    state, _ = scheduler.state_store.read()

    assert report["closed_agent_mr_feedback_count"] == 1
    assert state["closed_agent_mr_feedback"] == [
        {
            "iid": 18,
            "title": "Add smoke baseline",
            "source_branch": "agent/tests-02-smoke-baseline-run",
            "changed_files": ["tests/smoke.py"],
            "labels": ["agent/generated"],
            "closed_at": "2026-05-22T12:00:00Z",
            "reason": "baseline was flaky",
        }
    ]
    scheduler_report = json.loads((scheduler.artifacts.artifacts_dir / "scheduler_report.json").read_text(encoding="utf-8"))
    assert scheduler_report["closed_agent_mr_feedback"][0]["reason"] == "baseline was flaky"


def test_scheduler_watch_keeps_closed_feedback_when_closed_mr_api_fails(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="head1", fail_closed_mrs=True))
    state, _ = scheduler.state_store.read()
    state["closed_agent_mr_feedback"] = [{"iid": 18, "title": "old", "source_branch": "agent/old"}]
    scheduler.state_store.write(state)

    report = scheduler.watch()

    assert report["closed_agent_mr_feedback"] == [{"iid": 18, "title": "old", "source_branch": "agent/old"}]
    assert "closed MR list failed" in report["closed_agent_mr_feedback_warning"]
    assert "super-secret" not in report["closed_agent_mr_feedback_warning"]


def test_scheduler_watch_keeps_last_open_mr_count_when_mr_api_fails(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="head1", fail_open_mrs=True))
    state, _ = scheduler.state_store.read()
    state["open_agent_mrs"] = 2
    state["open_agent_mrs_details"] = [{"iid": 18, "title": "old", "source_branch": "agent/old"}]
    scheduler.state_store.write(state)

    report = scheduler.watch()

    assert report["status"] == "passed"
    assert report["open_agent_mrs_count"] == 2
    assert report["open_agent_mrs"] == [{"iid": 18, "title": "old", "source_branch": "agent/old"}]
    assert "GitLab API failed" in report["open_agent_mrs_warning"]
    assert "super-secret" not in report["open_agent_mrs_warning"]


def test_scheduler_watch_does_not_prepare_workspace(monkeypatch, tmp_path: Path) -> None:
    def fail_orchestrator(*args, **kwargs):
        raise AssertionError("watch should not construct Orchestrator")

    monkeypatch.setattr("agentlab.scheduler.Orchestrator", fail_orchestrator)
    scheduler = Scheduler(config(tmp_path), run_id="watch-only")
    scheduler._gitlab = lambda: FakeGitLab(head="head1", open_mrs=0)  # type: ignore[method-assign]

    report = scheduler.watch()

    assert report["status"] == "passed"


def test_scheduler_plan_skips_when_disabled(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path, schedule={"enabled": False}))

    report = scheduler.plan()

    assert report["status"] == "skipped"
    assert report["reason"] == "schedule_disabled"


def test_scheduler_plan_skips_unchanged_default_branch(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="same"))
    state, _ = scheduler.state_store.read()
    state["last_default_branch_head"] = "same"
    state["last_plan_run"] = "2026-05-20T00:00:00+00:00"
    scheduler.state_store.write(state)

    report = scheduler.plan()

    assert report["status"] == "skipped"
    assert report["reason"] == "default_branch_unchanged"


def test_scheduler_plan_applies_auto_approval(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(head="new"))

    report = scheduler.plan()

    assert report["status"] == "passed"
    assert report["selected_task_id"] == "docs-readme"
    auto = json.loads((scheduler.artifacts.artifacts_dir / "auto_approval_report.json").read_text(encoding="utf-8"))
    assert auto["selected_task_id"] == "docs-readme"
    state, _ = scheduler.state_store.read()
    assert state["last_plan_run_id"] == "run-1"


def test_scheduler_action_skips_limits_and_cooldown(tmp_path: Path) -> None:
    scheduler = HelperScheduler(
        config(tmp_path, schedule={"enabled": True, "behavior": {"skip_if_open_agent_mr_exists": False}}),
        gitlab=FakeGitLab(open_mrs=2),
    )
    assert scheduler.action()["reason"] == "open_agent_mr_limit_reached"

    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))
    state, _ = scheduler.state_store.read()
    state["new_mrs_today"] = 1
    state["new_mrs_date"] = datetime.now(UTC).date().isoformat()
    scheduler.state_store.write(state)
    assert scheduler.action()["reason"] == "daily_mr_limit_reached"


def test_scheduler_action_runs_one_flow_and_updates_mr_count(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))

    report = scheduler.action()

    assert report["status"] == "passed"
    assert scheduler.orchestrator.full_flow_called is True
    assert scheduler.orchestrator.full_flow_task_id is None
    state, _ = scheduler.state_store.read()
    assert state["new_mrs_today"] == 1
    assert state["last_action_run"]


def test_scheduler_action_passes_configured_preferences(tmp_path: Path) -> None:
    scheduler = HelperScheduler(
        config(
            tmp_path,
            schedule={
                "enabled": True,
                "action": {
                    "preferred_task_types": ["tests", "docs"],
                    "preferred_task_ids": ["tests-02-smoke-baseline"],
                },
            },
        ),
        gitlab=FakeGitLab(open_mrs=0),
    )

    scheduler.action()

    assert scheduler.orchestrator.full_flow_preferred_task_types == ["tests", "docs"]
    assert scheduler.orchestrator.full_flow_preferred_task_ids == ["tests-02-smoke-baseline"]


def test_scheduler_action_passes_closed_feedback_to_selection(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))
    state, _ = scheduler.state_store.read()
    state["closed_agent_mr_feedback"] = [{"iid": 18, "title": "Add smoke baseline", "changed_files": ["tests/smoke.py"]}]
    scheduler.state_store.write(state)

    report = scheduler.action()

    assert report["status"] == "passed"
    assert scheduler.orchestrator.full_flow_closed_agent_mr_feedback == [
        {"iid": 18, "title": "Add smoke baseline", "changed_files": ["tests/smoke.py"]}
    ]


def test_scheduler_action_cli_preferences_override_configured_preferences(tmp_path: Path) -> None:
    scheduler = HelperScheduler(
        config(
            tmp_path,
            schedule={
                "enabled": True,
                "action": {
                    "preferred_task_types": ["docs"],
                    "preferred_task_ids": ["docs-01-credentials"],
                },
            },
        ),
        gitlab=FakeGitLab(open_mrs=0),
    )

    scheduler.action(prefer_task_types=["tests"], prefer_task_ids=["tests-02-smoke-baseline"])

    assert scheduler.orchestrator.full_flow_preferred_task_types == ["tests"]
    assert scheduler.orchestrator.full_flow_preferred_task_ids == ["tests-02-smoke-baseline"]


def test_scheduler_action_with_task_id_selects_matching_approved_task(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))
    write_approved_plan(
        scheduler,
        TaskPlan(
            tasks=[
                task("docs-01-credentials", TaskType.DOCS, approved=True),
                task("tests-02-smoke-baseline", TaskType.TESTS, approved=True),
            ]
        ),
    )

    report = scheduler.action(task_id="tests-02-smoke-baseline")

    assert report["status"] == "passed"
    assert report["selected_task_id"] == "tests-02-smoke-baseline"
    assert scheduler.orchestrator.full_flow_task_id == "tests-02-smoke-baseline"
    assert [item.id for item in scheduler.orchestrator.full_flow_approved_plan.tasks] == [
        "docs-01-credentials",
        "tests-02-smoke-baseline",
    ]
    scheduler_report = json.loads((scheduler.artifacts.artifacts_dir / "scheduler_report.json").read_text(encoding="utf-8"))
    assert scheduler_report["selected_task_id"] == "tests-02-smoke-baseline"


def test_scheduler_action_with_unknown_task_id_fails_clearly(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))
    write_approved_plan(
        scheduler,
        TaskPlan(tasks=[task("docs-01-credentials", TaskType.DOCS, approved=True)]),
    )

    report = scheduler.action(task_id="missing-task")

    assert report["status"] == "failed"
    assert report["reason"] == "selected task not found in approved plan"
    assert report["selected_task_id"] == "missing-task"


def test_scheduler_action_with_rejected_task_id_fails_clearly(tmp_path: Path) -> None:
    scheduler = HelperScheduler(config(tmp_path), gitlab=FakeGitLab(open_mrs=0))
    write_approved_plan(
        scheduler,
        TaskPlan(tasks=[task("rejected-task", TaskType.TESTS, approved=False)]),
    )

    report = scheduler.action(task_id="rejected-task")

    assert report["status"] == "failed"
    assert report["reason"] == "selected task is not approved"
    assert report["selected_task_id"] == "rejected-task"


def test_scheduler_action_skips_no_auto_approved_task(tmp_path: Path) -> None:
    scheduler = HelperScheduler(
        config(tmp_path),
        result={"status": "blocked", "reason": "no approved task available for implementation"},
    )

    report = scheduler.action()

    assert report["status"] == "skipped"
    assert report["reason"] == "no_auto_approved_task"


def test_scheduler_reset_state_removes_existing_file(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = SchedulerStateStore(cfg.workspace_root)
    state, _ = store.read()
    state["last_default_branch_head"] = "abc"
    store.write(state)

    report = reset_scheduler_state(cfg)

    assert report["status"] == "passed"
    assert report["existed"] is True
    assert not store.path.exists()


def test_scheduler_reset_state_reports_missing_file(tmp_path: Path) -> None:
    cfg = config(tmp_path)

    report = reset_scheduler_state(cfg)

    assert report["reason"] == "scheduler_state_removed"
    assert report["existed"] is False


def test_scheduler_status_reads_state_without_writing(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = SchedulerStateStore(cfg.workspace_root)
    state, _ = store.read()
    state["last_default_branch_head"] = "abc"
    state["open_agent_mrs"] = 2
    store.write(state)

    report = scheduler_status(cfg)

    assert report["exists"] is True
    assert report["last_default_branch_head"] == "abc"
    assert report["open_agent_mrs"] == 2
