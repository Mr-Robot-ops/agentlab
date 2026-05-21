from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime

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
    def __init__(self, *, head: str = "abc", open_mrs: int = 0) -> None:
        self.head = head
        self.open_mrs = open_mrs

    def get_default_branch_head(self) -> str:
        return self.head

    def list_open_agent_mrs(self) -> list[object]:
        return [object() for _ in range(self.open_mrs)]


class FakeOrchestrator:
    def __init__(self, cfg: AppConfig, *, result: dict[str, object] | None = None) -> None:
        self.run_id = "run-1"
        from agentlab.artifacts import ArtifactStore

        self.artifacts = ArtifactStore(Path(cfg.workspace_root) / self.run_id, self.run_id)
        self.result = result or {"status": "passed", "merge_request": {"status": "created"}}
        self.plan_called = False
        self.full_flow_called = False

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

    def full_flow(self) -> dict[str, object]:
        self.full_flow_called = True
        return self.result


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


def test_schedule_defaults_disabled(tmp_path: Path) -> None:
    cfg = config(tmp_path, schedule={})

    assert cfg.schedule.enabled is False


def test_schedule_block_parsed(tmp_path: Path) -> None:
    cfg = config(tmp_path, schedule={"enabled": True, "watch": {"enabled": True, "cron": "*/5 * * * *"}})

    assert cfg.schedule.enabled is True
    assert cfg.schedule.watch.cron == "*/5 * * * *"


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
    state, _ = scheduler.state_store.read()
    assert state["new_mrs_today"] == 1
    assert state["last_action_run"]


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
