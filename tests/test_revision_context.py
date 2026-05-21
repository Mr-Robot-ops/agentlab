from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agentlab.config import AppConfig
from agentlab.models import AgentTask, CommandResult, GateDecision, ImplementationReport, ReportStatus
from agentlab.orchestrator import Orchestrator


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def config(tmp_path: Path, repo: Path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=repo,
        workspace_root=tmp_path / "runs",
        push_agent_branches_enabled=False,
        supply_chain_enabled=False,
        provenance_enabled=False,
        auto_approve={"enabled": True, "allowed_paths": ["README.md", "docs/**"]},
    )


def make_revision_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", remote, seed], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run_git(seed, "config", "user.email", "agentlab@example.com")
    run_git(seed, "config", "user.name", "AgentLab")
    run_git(seed, "checkout", "-b", "main")
    write(
        seed,
        "README.md",
        "# Demo\n\n## Project Structure\n\n```text\n.\n+-- rust-backend/\n|   +-- src/\n|       +-- routes/\n|           +-- health.rs\n|           +-- users.rs\n+-- web/\n    +-- src/\n        +-- App.tsx\n```\n",
    )
    write(seed, "rust-backend/src/routes/health.rs", "health\n")
    write(seed, "rust-backend/src/routes/users.rs", "users\n")
    write(seed, "web/src/App.tsx", "app\n")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-m", "initial")
    run_git(seed, "push", "origin", "main")

    run_git(seed, "checkout", "-b", "agent/docs")
    write(seed, "README.md", "# Demo\n\n## Project Structure\n\n```text\n.\n+-- rust-backend/\n+-- web/\n```\n")
    write(seed, "docs/new.md", "new MR file\n")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-m", "agent: simplify README structure")
    run_git(seed, "push", "origin", "agent/docs")

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", remote, repo], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run_git(repo, "checkout", "main")
    return repo


class CapturingImplementationAgent:
    captured: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.repo_context = kwargs.get("repo_context", {})
        CapturingImplementationAgent.captured["repo_context"] = self.repo_context

    def revise_on_branch(self, task: AgentTask, branch: str) -> ImplementationReport:
        CapturingImplementationAgent.captured["task"] = task
        CapturingImplementationAgent.captured["branch"] = branch
        return ImplementationReport(
            task_id=task.id,
            branch=branch,
            status=ReportStatus.PASSED,
            commit_sha="abc123",
            changed_files=["README.md"],
            implementation_mode="structured_edit",
        )


class CapturingProposalImplementationAgent:
    captured: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.artifacts = kwargs["artifacts"]
        CapturingProposalImplementationAgent.captured["repo_context"] = kwargs.get("repo_context", {})

    def propose_on_branch(self, task: AgentTask, branch: str) -> ImplementationReport:
        CapturingProposalImplementationAgent.captured["task"] = task
        CapturingProposalImplementationAgent.captured["branch"] = branch
        self.artifacts.write_text("proposed.diff", "diff --git a/README.md b/README.md\n")
        self.artifacts.write_json(
            "structured_proposal_report",
            {
                "status": "generated",
                "changed_files": ["README.md"],
                "added_lines": 1,
                "deleted_lines": 0,
                "secrets_touched": False,
                "touched_protected_paths": [],
                "proposal_artifacts": ["proposed.diff", "structured_proposal_report.json"],
            },
        )
        return ImplementationReport(
            task_id=task.id,
            branch=branch,
            status=ReportStatus.PASSED,
            applied=False,
            pushed=False,
            commit_sha=None,
            changed_files=["README.md"],
            patch_artifacts=["proposed.diff", "structured_proposal_report.json"],
            no_changes_committed=True,
            no_branch_pushed=True,
            implementation_mode="structured_edit",
        )

    def revise_on_branch(self, task: AgentTask, branch: str) -> ImplementationReport:
        raise AssertionError("propose-only must not use the real revision path")


def test_agent_revise_context_includes_base_and_mr_readme(monkeypatch, tmp_path: Path) -> None:
    repo = make_revision_repo(tmp_path)
    cfg = config(tmp_path, repo)
    previous_run = cfg.workspace_root / "previous" / "artifacts"
    previous_run.mkdir(parents=True)
    (previous_run / "project_structure_evidence.json").write_text(
        json.dumps({"validation_status": "blocked", "removed_existing_entries": ["rust-backend/src/routes/users.rs"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr("agentlab.orchestrator.ImplementationAgent", CapturingImplementationAgent)
    monkeypatch.setattr(
        Orchestrator,
        "review_and_gate",
        lambda self, task, direct_main_push=False: GateDecision(
            allowed=True,
            mode="merge_request",
            verdict="allowed",
            risk_score=1,
        ),
    )

    orchestrator = Orchestrator(cfg, run_id="revision-run")
    result = orchestrator.revise_existing_mr(
        mr_iid=15,
        source_branch="agent/docs",
        command="revise",
        feedback="Bitte Detailtiefe aus main wiederherstellen.",
        note_id=1,
        changed_files=["README.md", "docs/new.md"],
    )

    assert result["status"] == "passed"
    base_snapshot = json.loads((orchestrator.artifacts.artifacts_dir / "base_file_snapshot.json").read_text(encoding="utf-8"))
    mr_snapshot = json.loads((orchestrator.artifacts.artifacts_dir / "mr_file_snapshot.json").read_text(encoding="utf-8"))
    revision_context = json.loads((orchestrator.artifacts.artifacts_dir / "revision_context.json").read_text(encoding="utf-8"))
    revision_task = json.loads((orchestrator.artifacts.artifacts_dir / "revision_task.json").read_text(encoding="utf-8"))

    base_readme = next(item for item in base_snapshot["files"] if item["path"] == "README.md")
    mr_readme = next(item for item in mr_snapshot["files"] if item["path"] == "README.md")
    base_new_doc = next(item for item in base_snapshot["files"] if item["path"] == "docs/new.md")
    assert "users.rs" in base_readme["content"]
    assert "+-- rust-backend/\n+-- web/" in mr_readme["content"]
    assert base_new_doc["exists"] is False
    assert base_new_doc["content"] == ""

    summary = next(item for item in revision_context["structured_diff_summary"] if item["path"] == "README.md")
    assert "users.rs" in summary["base_branch_block"]
    assert "+-- rust-backend/\n+-- web/" in summary["current_mr_block"]
    assert summary["user_requested_change"] == "Bitte Detailtiefe aus main wiederherstellen."
    assert summary["intended_final_block"] == summary["base_branch_block"]
    assert revision_context["changed_files"] == ["README.md", "docs/new.md"]
    assert revision_context["previous_agent_commits"][0]["subject"] == "agent: simplify README structure"
    assert revision_context["previous_artifacts"][0]["name"] == "project_structure_evidence.json"

    captured_context = CapturingImplementationAgent.captured["repo_context"]["revision_context"]
    assert captured_context["structured_diff_summary"][0]["base_branch_block"]
    assert CapturingImplementationAgent.captured["task"].metadata["changed_files"] == ["README.md", "docs/new.md"]
    assert revision_task["metadata"]["changed_files"] == ["README.md", "docs/new.md"]


def test_agent_propose_only_uses_revision_context_without_gate_claim(monkeypatch, tmp_path: Path) -> None:
    repo = make_revision_repo(tmp_path)
    cfg = config(tmp_path, repo)

    monkeypatch.setattr("agentlab.orchestrator.ImplementationAgent", CapturingProposalImplementationAgent)
    monkeypatch.setattr(
        Orchestrator,
        "review_and_gate",
        lambda self, task, direct_main_push=False: (_ for _ in ()).throw(AssertionError("gate must not run for propose-only")),
    )

    orchestrator = Orchestrator(cfg, run_id="proposal-run")
    result = orchestrator.revise_existing_mr(
        mr_iid=15,
        source_branch="agent/docs",
        command="propose",
        feedback="Bitte nur vorschlagen.",
        note_id=2,
        changed_files=["README.md"],
        propose_only=True,
    )

    assert result["status"] == "passed"
    assert result["reason"] == "proposal_generated"
    assert result["propose_only"] is True
    assert result["commit_sha"] is None
    assert result["proposal_validation"]["status"] == "passed"
    assert "gate" not in result
    assert CapturingProposalImplementationAgent.captured["branch"] == "agent/docs"
    assert CapturingProposalImplementationAgent.captured["task"].metadata["revision_context"]


class DirtyAfterRestoreGit:
    def __init__(self) -> None:
        self.restored: list[str] = []
        self.calls = 0

    def status_porcelain(self) -> str:
        self.calls += 1
        return " M README.md"

    def restore_paths(self, paths: list[str]) -> CommandResult:
        self.restored = paths
        return CommandResult(command="git restore", cwd=".", exit_code=0)


def test_propose_cleanup_failure_reports_unclean_worktree(tmp_path: Path) -> None:
    repo = make_revision_repo(tmp_path)
    orchestrator = Orchestrator(config(tmp_path, repo), run_id="cleanup-run")
    git = DirtyAfterRestoreGit()

    cleanup = orchestrator._ensure_proposal_worktree_clean(git, ["README.md"])  # type: ignore[arg-type]

    assert cleanup["clean"] is False
    assert cleanup["cleanup_attempted"] is True
    assert git.restored == ["README.md"]
