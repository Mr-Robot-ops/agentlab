from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.review_comments import parse_review_command
from agentlab.scheduler import Scheduler, SchedulerStateStore


def config(tmp_path: Path, **overrides: object) -> AppConfig:
    values = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": tmp_path / "repo",
        "workspace_root": tmp_path / "runs",
        "push_agent_branches_enabled": True,
        "auto_approve": {"enabled": True},
        "schedule": {
            "enabled": True,
            "review_comments": {
                "enabled": True,
                "allowed_authors": ["alice"],
                "require_author_role": [],
            },
        },
    }
    values.update(overrides)
    Path(values["target_repo_path"]).mkdir(parents=True, exist_ok=True)
    return AppConfig.model_validate(values)


def mr(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 10,
        "iid": 15,
        "title": "Agent MR",
        "source_branch": "agent/docs",
        "target_branch": "main",
        "labels": ["agent/generated"],
        "state": "opened",
    }
    value.update(overrides)
    return value


def note(note_id: int, body: str, *, username: str = "alice", user_id: int = 2) -> dict[str, object]:
    return {
        "id": note_id,
        "body": body,
        "author": {"id": user_id, "username": username},
        "created_at": f"2026-05-21T00:00:{note_id:02d}Z",
        "system": False,
    }


class FakeGitLab:
    def __init__(
        self,
        *,
        mrs: list[dict[str, object]] | None = None,
        notes: list[dict[str, object]] | None = None,
        discussions: list[dict[str, object]] | None = None,
        current_user: dict[str, object] | None = None,
        allowed: bool = True,
    ) -> None:
        self.mrs = mrs if mrs is not None else [mr()]
        self.notes = notes if notes is not None else []
        self.discussions = discussions if discussions is not None else []
        self.current_user = current_user or {"id": 99, "username": "agentlab-bot"}
        self.allowed = allowed
        self.posted: list[tuple[int, str]] = []
        self.changes = ["README.md"]

    def list_open_agent_merge_requests(self) -> list[dict[str, object]]:
        return self.mrs

    def list_merge_request_notes(self, mr_iid: int) -> list[dict[str, object]]:
        return self.notes

    def list_merge_request_discussions(self, mr_iid: int) -> list[dict[str, object]]:
        return self.discussions

    def post_merge_request_note(self, mr_iid: int, body: str) -> dict[str, object]:
        self.posted.append((mr_iid, body))
        return {"id": len(self.posted), "body": body}

    def get_current_user(self) -> dict[str, object]:
        return self.current_user

    def author_is_allowed(self, author, *, allowed_authors, require_author_role) -> bool:
        username = str(author.get("username", "")).lower()
        return self.allowed and username in {item.lower() for item in allowed_authors}

    def get_merge_request_changes(self, mr_iid: int) -> list[str]:
        return self.changes


class FakeOrchestrator:
    def __init__(self, cfg: AppConfig, *, result: dict[str, object] | None = None) -> None:
        self.run_id = "run-1"
        self.artifacts = ArtifactStore(Path(cfg.workspace_root) / self.run_id, self.run_id)
        self.calls: list[dict[str, object]] = []
        self.result = result or {
            "run_id": "run-1",
            "status": "passed",
            "reason": "comment_processed",
            "source_branch": "agent/docs",
            "command": "revise",
            "commit_sha": "abc123",
            "changed_files": ["README.md"],
            "auto_approval": {"approved_tasks": ["mr-15-revise-1"], "evaluated_tasks": [{"details": {"risk_score": 1}}]},
            "gate": {"allowed": True, "verdict": "passed", "blockers": []},
        }

    def revise_existing_mr(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class ReviewScheduler(Scheduler):
    def __init__(
        self,
        cfg: AppConfig,
        *,
        gitlab: FakeGitLab,
        orchestrator: FakeOrchestrator | None = None,
        run_id: str = "run-1",
    ) -> None:
        self.config = cfg
        self.run_id = run_id
        self.run_dir = Path(cfg.workspace_root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_store = SchedulerStateStore(cfg.workspace_root)
        self.artifacts = ArtifactStore(self.run_dir, run_id)
        self.fake_gitlab = gitlab
        self._orchestrator = orchestrator

    def _gitlab(self) -> FakeGitLab:
        return self.fake_gitlab


@pytest.mark.parametrize("command", ["revise", "fix", "status", "explain", "stop", "resume"])
def test_parser_recognizes_agent_commands(command: str) -> None:
    parsed = parse_review_command(f"/agent {command}")

    assert parsed is not None
    assert parsed.command == command
    assert parsed.allowed is True


def test_parser_recognizes_alias_and_multiline_feedback() -> None:
    parsed = parse_review_command("@agentlab revise\nBitte README aktualisieren.")

    assert parsed is not None
    assert parsed.command == "revise"
    assert parsed.feedback == "Bitte README aktualisieren."


@pytest.mark.parametrize("body", ["normaler Kommentar", "/agent run rm -rf /", "/agent merge"])
def test_parser_ignores_or_rejects_unsafe_comments(body: str) -> None:
    parsed = parse_review_command(body)

    if body.startswith("/agent"):
        assert parsed is not None
        assert parsed.allowed is False
        assert parsed.reason == "command_not_allowed"
    else:
        assert parsed is None


def test_allowed_author_can_run_status_and_posts_response(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status")])
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["reason"] == "comment_processed"
    assert gitlab.posted
    assert "Run:" in gitlab.posted[0][1]


def test_unknown_author_is_rejected_and_deduped(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status", username="mallory")], allowed=False)
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["reason"] == "unauthorized_comment"
    assert "not authorized" in gitlab.posted[0][1]
    state, _ = scheduler.state_store.read()
    assert "1:15:1" in state["processed_review_comments"]


def test_bot_author_is_ignored_without_response(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status", username="agentlab-bot", user_id=99)])
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["reason"] == "no_agent_comment"
    assert gitlab.posted == []


def test_non_agent_mrs_are_ignored(tmp_path: Path) -> None:
    gitlab = FakeGitLab(mrs=[mr(source_branch="feature/manual")], notes=[note(1, "/agent status")])
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["reason"] == "no_agent_comment"
    assert gitlab.posted == []


def test_processed_note_is_not_processed_again(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status")])
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab)
    state, _ = scheduler.state_store.read()
    state["processed_review_comments"] = {"1:15:1": {"status": "passed"}}
    scheduler.state_store.write(state)

    report = scheduler.review_comments()

    assert report["reason"] == "already_processed"
    assert gitlab.posted == []


def test_stop_blocks_revision_until_resume(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    gitlab = FakeGitLab(notes=[note(1, "/agent stop\nIch uebernehme manuell.")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab)

    stop_report = scheduler.review_comments()

    assert stop_report["reason"] == "comment_processed"
    state, _ = scheduler.state_store.read()
    assert "1:15" in state["stopped_mrs"]

    gitlab.notes = [note(2, "/agent revise\nBitte fixen.")]
    revise_report = scheduler.review_comments()

    assert revise_report["reason"] == "mr_stopped"
    assert "stopped" in gitlab.posted[-1][1]

    gitlab.notes = [note(3, "/agent resume")]
    resume_report = scheduler.review_comments()

    assert resume_report["reason"] == "comment_processed"
    state, _ = scheduler.state_store.read()
    assert state["stopped_mrs"] == {}


def test_read_only_explain_does_not_call_revision(tmp_path: Path) -> None:
    orchestrator = FakeOrchestrator(config(tmp_path))
    gitlab = FakeGitLab(notes=[note(1, "/agent explain")])
    scheduler = ReviewScheduler(config(tmp_path), gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert orchestrator.calls == []
    assert "Changed files" in gitlab.posted[0][1]


def test_revision_command_uses_existing_source_branch(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    orchestrator = FakeOrchestrator(cfg)
    gitlab = FakeGitLab(notes=[note(1, "/agent revise\nBitte README aktualisieren.")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["commit_sha"] == "abc123"
    assert orchestrator.calls[0]["source_branch"] == "agent/docs"
    assert orchestrator.calls[0]["feedback"] == "Bitte README aktualisieren."
    assert "AgentLab processed `/agent revise`" in gitlab.posted[0][1]
    parsed = json.loads((scheduler.artifacts.artifacts_dir / "parsed_command.json").read_text(encoding="utf-8"))
    assert parsed["command"] == "revise"
