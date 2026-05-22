import json
from pathlib import Path

from agentlab.audit import AuditLogger
from agentlab.config import AppConfig
from agentlab.status import list_run_statuses, read_run_status


def config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "gitlab_url": "https://gitlab.example.com",
            "project_id": 1,
            "target_repo_path": tmp_path / "repo",
            "workspace_root": tmp_path / "runs",
        }
    )


def test_audit_logger_writes_live_status_and_events(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "runs" / "run-1" / "audit.jsonl", "run-1")

    audit.emit(agent="planner", action="plan", status="started")
    running = audit.read_status()
    assert running.state == "running"
    assert running.current_agent == "planner"
    assert running.agents["planner"].current_action == "plan"

    audit.emit(agent="planner", action="plan", status="succeeded")
    passed = audit.read_status()
    assert passed.state == "passed"
    assert passed.current_agent is None
    assert passed.agents["planner"].state == "passed"

    events = (tmp_path / "runs" / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 2


def test_blocked_status_is_distinct_from_failed(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "runs" / "run-2" / "audit.jsonl", "run-2")
    audit.emit(agent="gatekeeper", action="decide", status="blocked", metadata={"reason": "policy"})

    snapshot = audit.read_status()
    assert snapshot.state == "blocked"
    assert snapshot.agents["gatekeeper"].state == "blocked"


def test_audit_logger_mirrors_metadata_to_live_logs(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLAB_LIVE_EVENTS", "1")
    audit = AuditLogger(tmp_path / "runs" / "run-logs" / "audit.jsonl", "run-logs")

    audit.emit(
        agent="orchestrator",
        action="full_flow",
        status="started",
        metadata={"selected_task_id": "tests-02-smoke-baseline"},
    )

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["metadata"]["selected_task_id"] == "tests-02-smoke-baseline"


def test_status_reader_lists_runs(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    audit = AuditLogger(tmp_path / "runs" / "run-3" / "audit.jsonl", "run-3")
    audit.emit(agent="workspace", action="prepare", status="succeeded")

    snapshot = read_run_status(cfg, "run-3")
    listed = list_run_statuses(cfg)
    assert snapshot.run_id == "run-3"
    assert [item.run_id for item in listed] == ["run-3"]
