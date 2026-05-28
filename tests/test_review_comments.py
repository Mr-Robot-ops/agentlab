from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.policies.auto_approval import AutoApprovalPolicy
from agentlab.review_comments import parse_review_command
from agentlab.scheduler import Scheduler, SchedulerStateStore, reset_scheduler_state


def config(tmp_path: Path, *, process_history: bool = False, **overrides: object) -> AppConfig:
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
                "process_history": process_history,
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


@pytest.mark.parametrize("command", ["revise", "fix", "propose", "apply", "dry-run", "status", "merge-status", "explain", "stop", "resume"])
def test_parser_recognizes_agent_commands(command: str) -> None:
    parsed = parse_review_command(f"/agent {command}")

    assert parsed is not None
    assert parsed.command == command
    assert parsed.allowed is True
    assert parsed.propose_only is (command in {"propose", "dry-run"})


def test_parser_recognizes_alias_and_multiline_feedback() -> None:
    parsed = parse_review_command("@agentlab revise\nBitte README aktualisieren.")

    assert parsed is not None
    assert parsed.command == "revise"
    assert parsed.feedback == "Bitte README aktualisieren."


def test_parser_recognizes_revise_dry_run_and_cleans_feedback() -> None:
    parsed = parse_review_command("/agent revise --dry-run Bitte README aktualisieren.")

    assert parsed is not None
    assert parsed.command == "revise"
    assert parsed.propose_only is True
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
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["reason"] == "comment_processed"
    assert gitlab.posted
    assert "Run:" in gitlab.posted[0][1]


def test_unknown_author_is_rejected_and_deduped(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status", username="mallory")], allowed=False)
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["reason"] == "unauthorized_comment"
    assert "not authorized" in gitlab.posted[0][1]
    state, _ = scheduler.state_store.read()
    assert "1:15:1" in state["processed_review_comments"]


def test_merge_status_rejects_unauthorized_user(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent merge-status", username="mallory")], allowed=False)
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["reason"] == "unauthorized_comment"
    assert "not authorized" in gitlab.posted[0][1]


def test_bot_author_is_ignored_without_response(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent status", username="agentlab-bot", user_id=99)])
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

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
    cfg = config(tmp_path, process_history=True)
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
    orchestrator = FakeOrchestrator(config(tmp_path, process_history=True))
    gitlab = FakeGitLab(notes=[note(1, "/agent explain")])
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert orchestrator.calls == []
    assert "Changed files" in gitlab.posted[0][1]


def write_merge_status_artifacts(
    cfg: AppConfig,
    *,
    branch: str = "agent/docs",
    gate: dict[str, object] | None = None,
    functional: dict[str, object] | None = None,
    quality: dict[str, object] | None = None,
    security: dict[str, object] | None = None,
) -> None:
    artifacts = Path(cfg.workspace_root) / "gate-run" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "implementation_report.json").write_text(
        json.dumps({"task_id": "t1", "branch": branch, "status": "passed"}),
        encoding="utf-8",
    )
    (artifacts / "gate_decision.json").write_text(
        json.dumps(gate or {"allowed": True, "verdict": "allowed", "blockers": []}),
        encoding="utf-8",
    )
    (artifacts / "functional_test_report.json").write_text(
        json.dumps(functional or {"status": "passed", "passed": True}),
        encoding="utf-8",
    )
    (artifacts / "quality_review.json").write_text(
        json.dumps(quality or {"verdict": "approved", "summary": "ok"}),
        encoding="utf-8",
    )
    (artifacts / "security_architecture_review.json").write_text(
        json.dumps(security or {"verdict": "approved", "summary": "ok"}),
        encoding="utf-8",
    )


def test_merge_status_reports_manual_merge_safe_when_only_auto_merge_disabled(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True, auto_merge_enabled=False)
    write_merge_status_artifacts(cfg)
    gitlab = FakeGitLab(notes=[note(1, "/agent merge-status")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    response = gitlab.posted[0][1]
    assert "AgentLab merge status for this MR." in response
    assert "- Gate verdict: `allowed`" in response
    assert "- Blockers: `None`" in response
    assert "- Functional tests: `passed`" in response
    assert "- Quality review: `approved`" in response
    assert "- Security review: `approved`" in response
    assert "- Auto-merge: `disabled`" in response
    assert "- Recommendation: Manual merge is safe" in response


def test_merge_status_reports_do_not_merge_when_functional_tests_block(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True, auto_merge_enabled=False)
    write_merge_status_artifacts(
        cfg,
        gate={"allowed": False, "verdict": "blocked", "blockers": ["functional tests did not pass"]},
        functional={"status": "failed", "passed": False},
    )
    gitlab = FakeGitLab(notes=[note(1, "/agent merge-status")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    response = gitlab.posted[0][1]
    assert "- Gate verdict: `blocked`" in response
    assert "- Blockers: `functional tests did not pass`" in response
    assert "- Functional tests: `failed`" in response
    assert "- Recommendation: Do not merge yet" in response


def test_merge_status_explains_missing_artifacts(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(1, "/agent merge-status")])
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    response = gitlab.posted[0][1]
    assert "merge status for this MR is unavailable" in response
    assert "No AgentLab gate report artifacts were found" in response
    assert "Recommendation: Do not merge yet" in response


def test_revision_command_uses_existing_source_branch(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True)
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
    assert parsed["propose_only"] is False


def test_propose_command_generates_proposal_response_and_consumes_cooldown(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True)
    result = {
        "run_id": "run-1",
        "status": "passed",
        "reason": "proposal_generated",
        "source_branch": "agent/docs",
        "command": "propose",
        "propose_only": True,
        "commit_sha": None,
        "changed_files": ["README.md"],
        "auto_approval": {"approved_tasks": ["mr-15-propose-1"], "evaluated_tasks": [{"details": {"risk_score": 1}}]},
        "proposal_validation": {"status": "passed", "blockers": []},
        "proposal_artifacts": ["structured_proposal.json", "proposed.diff", "structured_proposal_report.json"],
    }
    orchestrator = FakeOrchestrator(cfg, result=result)
    gitlab = FakeGitLab(notes=[note(1, "/agent propose\nBitte README nur vorschlagen.")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["reason"] == "proposal_generated"
    assert orchestrator.calls[0]["propose_only"] is True
    assert orchestrator.calls[0]["feedback"] == "Bitte README nur vorschlagen."
    response = gitlab.posted[0][1]
    assert "AgentLab generated a proposed revision but did not push it." in response
    assert "Commit: none" in response
    assert "Push: skipped" in response
    assert "Proposal validation:" in response
    assert "Gate:" not in response
    assert "- structured_proposal.json" in response
    assert "- proposed.diff" in response
    assert "- structured_proposal_report.json" in response
    state, _ = scheduler.state_store.read()
    assert "1:15:1" in state["processed_review_comments"]
    assert "1:15" in state["review_comment_cooldowns"]


def test_apply_command_routes_to_revision_and_reports_proposal_run(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True)
    result = {
        "run_id": "run-1",
        "status": "passed",
        "reason": "proposal_applied",
        "source_branch": "agent/docs",
        "command": "apply",
        "proposal_run_id": "proposal-run",
        "commit_sha": "abc123",
        "changed_files": ["README.md"],
        "auto_approval": {"approved_tasks": ["mr-15-apply-1"], "evaluated_tasks": [{"details": {"risk_score": 1}}]},
        "gate": {"allowed": True, "verdict": "passed", "blockers": []},
    }
    orchestrator = FakeOrchestrator(cfg, result=result)
    gitlab = FakeGitLab(notes=[note(1, "/agent apply")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["reason"] == "proposal_applied"
    assert orchestrator.calls[0]["command"] == "apply"
    assert orchestrator.calls[0]["propose_only"] is False
    response = gitlab.posted[0][1]
    assert "AgentLab processed `/agent apply`" in response
    assert "Proposal run: proposal-run" in response


def test_revise_dry_run_routes_to_propose_only_without_gate_claim(tmp_path: Path) -> None:
    cfg = config(tmp_path, process_history=True)
    result = {
        "run_id": "run-1",
        "status": "passed",
        "reason": "proposal_generated",
        "source_branch": "agent/docs",
        "command": "revise",
        "propose_only": True,
        "changed_files": ["README.md"],
        "auto_approval": {"approved_tasks": ["mr-15-revise-1"], "evaluated_tasks": [{"details": {"risk_score": 1}}]},
        "proposal_validation": {"status": "failed", "blockers": ["docs check failed"]},
        "proposal_artifacts": ["structured_proposal.json", "proposed.diff", "structured_proposal_report.json"],
    }
    orchestrator = FakeOrchestrator(cfg, result=result)
    gitlab = FakeGitLab(notes=[note(1, "/agent revise --dry-run Bitte README pruefen.")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    scheduler.review_comments()

    assert orchestrator.calls[0]["propose_only"] is True
    assert orchestrator.calls[0]["feedback"] == "Bitte README pruefen."
    response = gitlab.posted[0][1]
    assert "Proposal validation:" in response
    assert "- failed" in response
    assert "MR gate passed" not in response
    assert "Gate:" not in response


def test_empty_state_initializes_seen_notes_without_processing_history(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    orchestrator = FakeOrchestrator(cfg)
    gitlab = FakeGitLab(notes=[note(41, "/agent status"), note(43, "/agent revise\nBitte neu pruefen.")])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    report = scheduler.review_comments()

    assert report["status"] == "skipped"
    assert report["reason"] == "review_comments_initialized"
    assert gitlab.posted == []
    assert orchestrator.calls == []
    state, _ = scheduler.state_store.read()
    assert state["processed_review_comments"] == {}
    assert state["review_comments_seen"]["1:15"]["last_seen_note_id"] == 43


def test_new_comment_after_initialization_is_processed(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    orchestrator = FakeOrchestrator(cfg)
    old_status = note(41, "/agent status")
    gitlab = FakeGitLab(notes=[old_status])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    first = scheduler.review_comments()
    gitlab.notes = [old_status, note(44, "/agent revise\nBitte README aktualisieren.")]
    second = scheduler.review_comments()

    assert first["reason"] == "review_comments_initialized"
    assert second["status"] == "passed"
    assert orchestrator.calls[0]["note_id"] == 44
    assert orchestrator.calls[0]["feedback"] == "Bitte README aktualisieren."
    assert "AgentLab processed `/agent revise`" in gitlab.posted[0][1]
    state, _ = scheduler.state_store.read()
    assert state["review_comments_seen"]["1:15"]["last_seen_note_id"] == 44


def test_process_history_true_processes_historical_commands(tmp_path: Path) -> None:
    gitlab = FakeGitLab(notes=[note(41, "/agent status")])
    scheduler = ReviewScheduler(config(tmp_path, process_history=True), gitlab=gitlab)

    report = scheduler.review_comments()

    assert report["status"] == "passed"
    assert report["reason"] == "comment_processed"
    assert "Run:" in gitlab.posted[0][1]


def test_state_reset_does_not_replay_old_status_before_new_revise(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    state_store = SchedulerStateStore(cfg.workspace_root)
    state, _ = state_store.read()
    state["processed_review_comments"] = {"1:15:41": {"status": "passed"}}
    state_store.write(state)
    reset_scheduler_state(cfg)

    orchestrator = FakeOrchestrator(cfg)
    old_status = note(41, "/agent status")
    gitlab = FakeGitLab(notes=[old_status])
    scheduler = ReviewScheduler(cfg, gitlab=gitlab, orchestrator=orchestrator)

    first = scheduler.review_comments()
    gitlab.notes = [old_status, note(44, "/agent revise\nBitte reparieren.")]
    second = scheduler.review_comments()

    assert first["reason"] == "review_comments_initialized"
    assert second["status"] == "passed"
    assert orchestrator.calls[0]["command"] == "revise"
    assert orchestrator.calls[0]["note_id"] == 44
    assert len(gitlab.posted) == 1
    assert "AgentLab processed `/agent revise`" in gitlab.posted[0][1]
    assert "AgentLab status for this MR" not in gitlab.posted[0][1]


def policy_task(**overrides: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "mr-15-revise-1",
        "title": "Revise MR !15 from review comment",
        "task_type": TaskType.DOCS,
        "risk_level": RiskLevel.LOW,
        "risk_score": 1,
        "affected_files": ["README.md"],
        "approved": False,
    }
    values.update(overrides)
    return AgentTask.model_validate(values)


def policy_blocked_revision(cfg: AppConfig, task: AgentTask) -> dict[str, object]:
    _, report = AutoApprovalPolicy(cfg).apply(TaskPlan(tasks=[task]))
    return {
        "status": "failed",
        "reason": "policy_blocked",
        "command": "revise",
        "source_branch": "agent/docs",
        "changed_files": task.affected_files,
        "task": task.model_dump(mode="json"),
        "auto_approval": report,
    }


def revision_response(tmp_path: Path, task: AgentTask, **config_overrides: object) -> str:
    cfg = config(tmp_path, **config_overrides)
    scheduler = ReviewScheduler(cfg, gitlab=FakeGitLab())
    return scheduler._revision_response("revise", policy_blocked_revision(cfg, task))


def test_policy_blocked_response_names_auto_approve_disabled(tmp_path: Path) -> None:
    response = revision_response(tmp_path, policy_task(), auto_approve={"enabled": False})

    assert "Reason: policy_blocked" in response
    assert "- auto_approve_disabled" in response
    assert "- enabled: false" in response
    assert "- task_type: docs" in response
    assert "Disallowed paths:" not in response
    assert "disallowed_paths:\n  - <none>" not in response


def test_policy_blocked_response_names_path_not_allowed_with_paths(tmp_path: Path) -> None:
    response = revision_response(tmp_path, policy_task(affected_files=["src/app.py"]))

    assert "- path_not_allowed" in response
    assert "- disallowed_paths:" in response
    assert "  - src/app.py" in response
    assert "Disallowed paths:\n- None" not in response


def test_policy_blocked_response_names_task_type_not_allowed(tmp_path: Path) -> None:
    response = revision_response(tmp_path, policy_task(task_type=TaskType.CI))

    assert "- task_type_not_allowed" in response
    assert "- task_type: ci" in response
    assert "- allowed_task_types:" in response
    assert "  - docs" in response
    assert "  - tests" in response


def test_policy_blocked_response_names_risk_score_too_high(tmp_path: Path) -> None:
    response = revision_response(tmp_path, policy_task(risk_score=99))

    assert "- risk_score_too_high" in response
    assert "- risk_score: 99" in response
