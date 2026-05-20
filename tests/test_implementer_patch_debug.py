from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.agents.implementer import ImplementationAgent
from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    ArchitectureSummary,
    CommandResult,
    DiffStats,
    ImplementationReport,
    PatchProposal,
    RepoIndex,
    ReportStatus,
)
from agentlab.orchestrator import Orchestrator
from agentlab.tools.file_tool import PatchApplyError


PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # AgentLab
+More docs
"""


class FakeGitTool:
    def __init__(self) -> None:
        self.committed = False
        self.pushed = False

    def create_branch(self, branch: str, base: str) -> CommandResult:
        return CommandResult(command=f"git checkout -B {branch} {base}", cwd=".", exit_code=0)

    def commit(self, message: str) -> str:
        self.committed = True
        return "abc123"

    def push(self, branch: str) -> CommandResult:
        self.pushed = True
        return CommandResult(command=f"git push origin {branch}", cwd=".", exit_code=0)


class FakeFileTool:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.apply_calls = 0

    def read_file(self, path: str) -> str:
        return "# AgentLab\n"

    def validate_patch(self, proposal: PatchProposal) -> DiffStats:
        return DiffStats(changed_files=["README.md"], added_lines=1)

    def apply_patch(self, proposal: PatchProposal) -> DiffStats:
        self.apply_calls += 1
        if self.apply_calls <= self.fail_times:
            raise PatchApplyError(
                command=["git", "apply", "--check", "--whitespace=nowarn", "-"],
                stderr="error: corrupt patch at line 21\n",
                patch=proposal.patch,
                check=True,
            )
        return DiffStats(changed_files=["README.md"], added_lines=1)


class FakeOllama:
    def __init__(self, proposals: list[PatchProposal]) -> None:
        self.proposals = proposals
        self.calls = 0
        self.prompts: list[str] = []

    def chat_json_with_raw(self, **kwargs: Any) -> tuple[PatchProposal, str]:
        self.prompts.append(kwargs["user_prompt"])
        proposal = self.proposals[self.calls]
        self.calls += 1
        return proposal, proposal.model_dump_json()


def config(repo: Path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id="group/project",
        target_repo_path=repo,
        workspace_root=repo.parent / "runs",
        supply_chain_enabled=False,
        provenance_enabled=False,
    )


def task() -> AgentTask:
    return AgentTask(
        id="document-privileged-container-boundaries",
        title="Document privileged container boundaries",
        description="Document privileged container boundaries.",
        affected_files=["README.md"],
        approved=True,
    )


def proposal(summary: str = "docs") -> PatchProposal:
    return PatchProposal(
        task_id="document-privileged-container-boundaries",
        summary=summary,
        patch=PATCH,
        affected_files=["README.md"],
        expected_tests=[],
        rollback="Revert README.md changes.",
    )


def read_artifact(store: ArtifactStore, name: str) -> str:
    return (store.artifacts_dir / name).read_text(encoding="utf-8")


def test_malformed_patch_writes_debug_artifacts_and_failed_report(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=2)
    ollama = FakeOllama([proposal(), proposal("repair")])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_stage == "patch_apply"
    assert report.failure_reason == "corrupt_patch"
    assert report.retry_attempted is True
    assert report.retry_succeeded is False
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert git.committed is False
    assert git.pushed is False
    assert file_tool.apply_calls == 2
    assert ollama.calls == 2
    assert "raw_patch.diff" in report.patch_artifacts
    assert "patch_apply_error.txt" in report.patch_artifacts
    assert "patch_apply_stderr.txt" in report.patch_artifacts
    assert "patch_apply_command.json" in report.patch_artifacts
    assert "patch_excerpt.txt" in report.patch_artifacts
    assert "corrupt patch at line 21" in read_artifact(store, "patch_apply_error.txt")
    assert read_artifact(store, "patch_excerpt.txt").splitlines() == PATCH.splitlines()[:80]
    command = json.loads(read_artifact(store, "patch_apply_command.json"))
    assert command["command"] == ["git", "apply", "--check", "--whitespace=nowarn", "-"]


def test_corrupt_patch_repair_success_continues_to_commit(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=1)
    ollama = FakeOllama([proposal(), proposal("repair")])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(task())

    assert report.status == ReportStatus.PASSED
    assert report.retry_attempted is True
    assert report.retry_succeeded is True
    assert git.committed is True
    assert git.pushed is False
    assert file_tool.apply_calls == 2
    assert ollama.calls == 2


def test_valid_patch_behavior_still_works(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=0)
    ollama = FakeOllama([proposal()])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(task())

    assert report.status == ReportStatus.PASSED
    assert report.commit_sha == "abc123"
    assert report.changed_files == ["README.md"]
    assert report.retry_attempted is False
    assert report.patch_artifacts == ["implementer_raw_response.json", "patch_proposal.json", "raw_patch.diff"]


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class FakeArtifacts:
    def __init__(self) -> None:
        self.payloads: dict[str, Any] = {}

    def write_json(self, name: str, payload: Any) -> None:
        self.payloads[name] = payload


class RunTaskOrchestrator(Orchestrator):
    def __init__(self, cfg: AppConfig) -> None:
        self.config = cfg
        self.dry_run = False
        self.run_id = "run"
        self.audit = FakeAudit()
        self.artifacts = FakeArtifacts()
        self.ollama = object()

    def preflight(self, mode: str, *, enforce: bool = True) -> object:
        return object()

    def index_repository(self) -> tuple[RepoIndex, ArchitectureSummary]:
        return RepoIndex(root_path=".", total_files=1, indexed_files=1), ArchitectureSummary()

    def _tools(self) -> tuple[FakeGitTool, FakeFileTool, object, object]:  # type: ignore[override]
        return FakeGitTool(), FakeFileTool(), object(), object()


def test_run_task_marks_implementer_event_failed_when_report_failed(tmp_path: Path, monkeypatch: Any) -> None:
    class FailingImplementationAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def implement(self, task: AgentTask) -> ImplementationReport:
            return ImplementationReport(
                task_id=task.id,
                branch=f"agent/{task.id}",
                status=ReportStatus.FAILED,
                errors=["error: corrupt patch at line 21\n"],
                failure_stage="patch_apply",
                failure_reason="corrupt_patch",
            )

    monkeypatch.setattr("agentlab.orchestrator.ImplementationAgent", FailingImplementationAgent)
    orchestrator = RunTaskOrchestrator(config(tmp_path))

    report = orchestrator.run_task(task())

    assert report.status == ReportStatus.FAILED
    finished = orchestrator.audit.events[-1]
    assert finished["agent"] == "implementer"
    assert finished["status"] == "failed"
    assert finished["metadata"]["implementation_status"] == "failed"
    assert finished["metadata"]["implementation_error_count"] == 1
