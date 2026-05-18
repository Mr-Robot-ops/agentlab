from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import RunStatusSnapshot


TERMINAL_STATES = {"passed", "failed", "blocked"}


def status_path(config: AppConfig, run_id: str) -> Path:
    return Path(config.workspace_root) / run_id / "status.json"


def read_run_status(config: AppConfig, run_id: str) -> RunStatusSnapshot:
    path = status_path(config, run_id)
    if not path.exists():
        raise FileNotFoundError(f"run status not found: {path}")
    return RunStatusSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def list_run_statuses(config: AppConfig, *, limit: int = 20) -> list[RunStatusSnapshot]:
    root = Path(config.workspace_root)
    if not root.exists():
        return []
    snapshots: list[RunStatusSnapshot] = []
    for path in root.glob("*/status.json"):
        try:
            snapshots.append(RunStatusSnapshot.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return sorted(snapshots, key=lambda item: item.updated_at, reverse=True)[:limit]


def format_status(snapshot: RunStatusSnapshot) -> str:
    current = (
        f"{snapshot.current_agent}:{snapshot.current_action}"
        if snapshot.current_agent and snapshot.current_action
        else "-"
    )
    lines = [
        f"Run: {snapshot.run_id}",
        f"State: {snapshot.state}",
        f"Current: {current}",
        f"Updated: {snapshot.updated_at.isoformat()}",
        "",
        "Agents:",
    ]
    for agent in sorted(snapshot.agents.values(), key=lambda item: item.agent):
        action = agent.current_action or agent.last_action or "-"
        error = f" error={agent.last_error}" if agent.last_error else ""
        lines.append(f"- {agent.agent}: {agent.state} action={action} events={agent.event_count}{error}")
    return "\n".join(lines)
