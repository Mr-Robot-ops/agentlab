from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import agentlab.main as main


runner = CliRunner()


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("schedule: {}\n", encoding="utf-8")
    return path


def test_scheduler_action_task_id_failure_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    class FailingScheduler:
        def __init__(self, cfg: object) -> None:
            self.cfg = cfg

        def action(
            self,
            *,
            task_id: str | None = None,
            prefer_task_types: list[str] | None = None,
            prefer_task_ids: list[str] | None = None,
        ) -> dict[str, object]:
            return {
                "status": "failed",
                "reason": "selected task not found in approved plan",
                "selected_task_id": task_id,
            }

    monkeypatch.setattr(main, "load_config", lambda path: object())
    monkeypatch.setattr(main, "Scheduler", FailingScheduler)

    result = runner.invoke(
        main.app,
        ["scheduler-action", "--config", str(_config_file(tmp_path)), "--task-id", "missing-task"],
    )

    assert result.exit_code == 1
    assert '"selected_task_id": "missing-task"' in result.output


def test_scheduler_action_without_task_id_preserves_existing_exit(monkeypatch, tmp_path: Path) -> None:
    class FailingScheduler:
        def __init__(self, cfg: object) -> None:
            self.cfg = cfg

        def action(
            self,
            *,
            task_id: str | None = None,
            prefer_task_types: list[str] | None = None,
            prefer_task_ids: list[str] | None = None,
        ) -> dict[str, object]:
            return {"status": "failed", "reason": "gitlab_unavailable", "selected_task_id": task_id}

    monkeypatch.setattr(main, "load_config", lambda path: object())
    monkeypatch.setattr(main, "Scheduler", FailingScheduler)

    result = runner.invoke(main.app, ["scheduler-action", "--config", str(_config_file(tmp_path))])

    assert result.exit_code == 0
    assert '"status": "failed"' in result.output


def test_scheduler_action_passes_preference_flags(monkeypatch, tmp_path: Path) -> None:
    class CapturingScheduler:
        def __init__(self, cfg: object) -> None:
            self.cfg = cfg

        def action(
            self,
            *,
            task_id: str | None = None,
            prefer_task_types: list[str] | None = None,
            prefer_task_ids: list[str] | None = None,
        ) -> dict[str, object]:
            return {
                "status": "passed",
                "reason": "action_completed",
                "selected_task_id": "tests-02-smoke-baseline",
                "task_selection_reason": "preferred_task_type:tests",
                "prefer_task_types": prefer_task_types,
                "prefer_task_ids": prefer_task_ids,
            }

    monkeypatch.setattr(main, "load_config", lambda path: object())
    monkeypatch.setattr(main, "Scheduler", CapturingScheduler)

    result = runner.invoke(
        main.app,
        [
            "scheduler-action",
            "--config",
            str(_config_file(tmp_path)),
            "--prefer-task-type",
            "tests",
            "--prefer-task-type",
            "docs",
            "--prefer-task-id",
            "tests-02-smoke-baseline",
        ],
    )

    assert result.exit_code == 0
    assert '"task_selection_reason": "preferred_task_type:tests"' in result.output
    assert '"prefer_task_types": [\n    "tests",\n    "docs"\n  ]' in result.output
    assert '"prefer_task_ids": [\n    "tests-02-smoke-baseline"\n  ]' in result.output


def test_scheduler_plan_passes_focus_and_preference_flags(monkeypatch, tmp_path: Path) -> None:
    class CapturingScheduler:
        def __init__(self, cfg: object) -> None:
            self.cfg = cfg

        def plan(
            self,
            *,
            focus: str | None = None,
            prefer_task_types: list[str] | None = None,
            prefer_task_ids: list[str] | None = None,
        ) -> dict[str, object]:
            return {
                "status": "passed",
                "reason": "plan_completed",
                "focus": focus,
                "prefer_task_types": prefer_task_types,
                "prefer_task_ids": prefer_task_ids,
            }

    monkeypatch.setattr(main, "load_config", lambda path: object())
    monkeypatch.setattr(main, "Scheduler", CapturingScheduler)

    result = runner.invoke(
        main.app,
        [
            "scheduler-plan",
            "--config",
            str(_config_file(tmp_path)),
            "--focus",
            "rust smoke test",
            "--prefer-task-type",
            "tests",
            "--prefer-task-id",
            "rust-public-seam-smoke-test",
        ],
    )

    assert result.exit_code == 0
    assert '"focus": "rust smoke test"' in result.output
    assert '"prefer_task_types": [\n    "tests"\n  ]' in result.output
    assert '"prefer_task_ids": [\n    "rust-public-seam-smoke-test"\n  ]' in result.output
