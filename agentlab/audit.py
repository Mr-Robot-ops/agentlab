from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import AgentRunStatus, AuditEvent, RunStatusSnapshot


SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]+",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
]
SECRET_KEYWORDS = ("token", "password", "secret", "api_key", "apikey", "private_key")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("REDACTED", redacted)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if any(keyword in str(key).lower() for keyword in SECRET_KEYWORDS):
                redacted[key] = "REDACTED"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    return value


class AuditLogger:
    def __init__(self, path: str | Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.events_path = self.path.parent / "events.jsonl"
        self.status_path = self.path.parent / "status.json"
        self.mirror_events_to_stderr = os.environ.get("AGENTLAB_LIVE_EVENTS", "1").lower() not in {"0", "false", "no"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_status()

    def emit(
        self,
        *,
        agent: str,
        action: str,
        status: str,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        duration_seconds: float | None = None,
        input_payload: Any | None = None,
        output_payload: Any | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            run_id=self.run_id,
            agent=agent,
            action=action,
            status=status,  # type: ignore[arg-type]
            duration_seconds=duration_seconds,
            input_hash=stable_hash(input_payload) if input_payload is not None else None,
            output_hash=stable_hash(output_payload) if output_payload is not None else None,
            metadata=redact_secrets(metadata or {}),
            error=redact_secrets(error) if error else None,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        self._update_status(event)
        self._mirror_event(event)
        return event

    @contextmanager
    def span(self, *, agent: str, action: str, input_payload: Any | None = None) -> Iterator[None]:
        start = time.monotonic()
        self.emit(agent=agent, action=action, status="started", input_payload=input_payload)
        try:
            yield
        except Exception as exc:
            self.emit(
                agent=agent,
                action=action,
                status="failed",
                error=str(exc),
                duration_seconds=time.monotonic() - start,
            )
            raise
        self.emit(agent=agent, action=action, status="succeeded", duration_seconds=time.monotonic() - start)

    def read_status(self) -> RunStatusSnapshot:
        return RunStatusSnapshot.model_validate_json(self.status_path.read_text(encoding="utf-8"))

    def _ensure_status(self) -> None:
        if self.status_path.exists():
            return
        snapshot = RunStatusSnapshot(
            run_id=self.run_id,
            audit_file=str(self.path),
            events_file=str(self.events_path),
        )
        self._write_status(snapshot)

    def _update_status(self, event: AuditEvent) -> None:
        snapshot = self.read_status()
        agent_status = snapshot.agents.get(event.agent) or AgentRunStatus(agent=event.agent)
        agent_status.event_count += 1
        agent_status.last_action = event.action
        agent_status.updated_at = event.timestamp
        agent_status.last_error = event.error

        if event.status == "started":
            agent_status.state = "running"
            agent_status.current_action = event.action
            agent_status.finished_at = None
            if agent_status.started_at is None:
                agent_status.started_at = event.timestamp
        elif event.status == "succeeded":
            agent_status.state = "passed"
            agent_status.current_action = None
            agent_status.finished_at = event.timestamp
        elif event.status == "failed":
            agent_status.state = "failed"
            agent_status.current_action = None
            agent_status.finished_at = event.timestamp
        elif event.status == "skipped":
            agent_status.state = "skipped"
            agent_status.current_action = None
            agent_status.finished_at = event.timestamp
        elif event.status == "blocked":
            agent_status.state = "blocked"
            agent_status.current_action = None
            agent_status.finished_at = event.timestamp

        agents = dict(snapshot.agents)
        agents[event.agent] = agent_status
        running = [agent for agent in agents.values() if agent.state == "running"]
        failed = [agent for agent in agents.values() if agent.state == "failed"]
        blocked = [agent for agent in agents.values() if agent.state == "blocked"]

        if failed:
            run_state = "failed"
        elif running:
            run_state = "running"
        elif blocked:
            run_state = "blocked"
        elif agents:
            run_state = "passed"
        else:
            run_state = "pending"

        snapshot = snapshot.model_copy(
            update={
                "state": run_state,
                "updated_at": event.timestamp,
                "finished_at": event.timestamp if run_state in {"passed", "failed"} else None,
                "current_agent": running[-1].agent if running else None,
                "current_action": running[-1].current_action if running else None,
                "agents": agents,
                "last_event": event,
            }
        )
        self._write_status(snapshot)

    def _write_status(self, snapshot: RunStatusSnapshot) -> None:
        tmp_path = self.status_path.with_suffix(".json.tmp")
        tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(self.status_path)

    def _mirror_event(self, event: AuditEvent) -> None:
        if not self.mirror_events_to_stderr:
            return
        payload = {
            "type": "agentlab.event",
            "run_id": event.run_id,
            "agent": event.agent,
            "action": event.action,
            "status": event.status,
            "timestamp": event.timestamp.isoformat(),
            "error": event.error,
            "metadata": event.metadata,
        }
        print(json.dumps(payload, ensure_ascii=True), file=sys.stderr, flush=True)
