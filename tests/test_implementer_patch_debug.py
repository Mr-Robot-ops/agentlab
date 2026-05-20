from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentlab.artifacts import ArtifactStore
from agentlab.agents.implementer import ImplementationAgent
from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    ArchitectureSummary,
    CommandResult,
    DiffStats,
    FileEdit,
    ImplementationReport,
    PatchProposal,
    RepoIndex,
    ReportStatus,
    StructuredEditProposal,
    TaskType,
)
from agentlab.orchestrator import Orchestrator
from agentlab.tools.common import ToolError
from agentlab.tools.file_tool import FileTool, PatchApplyError, UnifiedDiffValidationError, validate_unified_diff_structure


PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # AgentLab
+More docs
"""

INVALID_RULE_PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,4 @@
 # AgentLab
---
+
+Next line
"""

INVALID_HEADING_PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,3 @@
 # AgentLab
## Security & Deployment Assumptions
+Next line
"""

VALID_MARKDOWN_PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,4 @@
 # AgentLab
+---
+
+## Security & Deployment Assumptions
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


class ValidatingFakeFileTool(FakeFileTool):
    def validate_patch(self, proposal: PatchProposal) -> DiffStats:
        validate_unified_diff_structure(proposal.patch)
        return DiffStats(changed_files=["README.md"], added_lines=1)

    def apply_patch(self, proposal: PatchProposal) -> DiffStats:
        self.validate_patch(proposal)
        return super().apply_patch(proposal)


class FakeOllama:
    def __init__(self, proposals: list[PatchProposal | StructuredEditProposal]) -> None:
        self.proposals = proposals
        self.calls = 0
        self.prompts: list[str] = []
        self.response_models: list[type[Any]] = []

    def chat_json_with_raw(self, **kwargs: Any) -> tuple[Any, str]:
        self.response_models.append(kwargs["response_model"])
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


def docs_task() -> AgentTask:
    return task().model_copy(update={"task_type": TaskType.DOCS})


def patch_task() -> AgentTask:
    return task().model_copy(update={"task_type": TaskType.BUGFIX, "affected_files": []})


def proposal(summary: str = "docs") -> PatchProposal:
    return proposal_with_patch(PATCH, summary=summary)


def proposal_with_patch(patch: str, summary: str = "docs") -> PatchProposal:
    return PatchProposal(
        task_id="document-privileged-container-boundaries",
        summary=summary,
        patch=patch,
        affected_files=["README.md"],
        expected_tests=[],
        rollback="Revert README.md changes.",
    )


def structured_proposal(*, path: str = "README.md", old_text: str = "# AgentLab\n", new_text: str = "# AgentLab\n\nMore docs\n") -> StructuredEditProposal:
    return StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="structured docs",
        edits=[FileEdit(path=path, operation="replace_text", old_text=old_text, new_text=new_text)],
        expected_tests=[],
        rollback="Revert README.md changes.",
    )


def read_artifact(store: ArtifactStore, name: str) -> str:
    return (store.artifacts_dir / name).read_text(encoding="utf-8")


def init_repo(tmp_path: Path, content: str = "# AgentLab\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (repo / "README.md").write_text(content, encoding="utf-8")
    return repo


def file_tool_for(repo: Path) -> FileTool:
    return FileTool(repo, config(repo))


def test_unified_diff_validation_rejects_unprefixed_markdown_rule_in_hunk() -> None:
    try:
        validate_unified_diff_structure(INVALID_RULE_PATCH)
    except UnifiedDiffValidationError as exc:
        assert exc.reason == "missing_diff_prefix_in_hunk"
        assert exc.offending_line == "---"
        assert exc.line_number == 6
    else:
        raise AssertionError("expected UnifiedDiffValidationError")


def test_unified_diff_validation_rejects_unprefixed_heading_in_hunk() -> None:
    try:
        validate_unified_diff_structure(INVALID_HEADING_PATCH)
    except UnifiedDiffValidationError as exc:
        assert exc.reason == "missing_diff_prefix_in_hunk"
        assert exc.offending_line == "## Security & Deployment Assumptions"
        assert exc.line_number == 6
    else:
        raise AssertionError("expected UnifiedDiffValidationError")


def test_unified_diff_validation_accepts_prefixed_markdown_additions() -> None:
    validate_unified_diff_structure(VALID_MARKDOWN_PATCH)


def test_structured_replace_text_updates_readme(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    stats = file_tool_for(repo).apply_structured_edits(structured_proposal())

    assert (repo / "README.md").read_text(encoding="utf-8") == "# AgentLab\n\nMore docs\n"
    assert stats.changed_files == ["README.md"]
    assert stats.added_lines >= 1


def test_structured_replace_text_fails_when_old_text_missing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    with pytest.raises(ToolError, match="old_text not found"):
        file_tool_for(repo).apply_structured_edits(structured_proposal(old_text="missing"))


def test_structured_replace_text_fails_when_old_text_repeats(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, content="repeat\nrepeat\n")

    with pytest.raises(ToolError, match="multiple times"):
        file_tool_for(repo).apply_structured_edits(structured_proposal(old_text="repeat", new_text="once"))


def test_structured_append_to_file_appends_existing_file(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="append",
        edits=[FileEdit(path="README.md", operation="append_to_file", content="\nAppendix\n")],
        rollback="Remove appended docs.",
    )

    stats = file_tool_for(repo).apply_structured_edits(proposal)

    assert (repo / "README.md").read_text(encoding="utf-8").endswith("\nAppendix\n")
    assert stats.changed_files == ["README.md"]


def test_structured_replace_file_replaces_content(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="replace",
        edits=[FileEdit(path="README.md", operation="replace_file", content="All new\n")],
        rollback="Restore README.md.",
    )

    stats = file_tool_for(repo).apply_structured_edits(proposal)

    assert (repo / "README.md").read_text(encoding="utf-8") == "All new\n"
    assert stats.changed_files == ["README.md"]


def test_structured_edit_rejects_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows symlink permissions vary by environment")
    repo = init_repo(tmp_path)
    (repo / "target.md").write_text("target\n", encoding="utf-8")
    (repo / "linked.md").symlink_to(repo / "target.md")
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="replace",
        edits=[FileEdit(path="linked.md", operation="replace_file", content="new\n")],
        rollback="Restore link target.",
    )

    with pytest.raises(ToolError, match="symlink"):
        file_tool_for(repo).apply_structured_edits(proposal)


def test_docs_task_uses_structured_edit_instead_of_patch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    ollama = FakeOllama([structured_proposal()])

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), ollama, artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.PASSED
    assert report.implementation_mode == "structured_edit"
    assert ollama.response_models == [StructuredEditProposal]
    assert "structured_edit_raw_response.json" in report.patch_artifacts
    assert "structured_edit_proposal.json" in report.patch_artifacts
    assert "structured_edit_apply_report.json" in report.patch_artifacts


def test_structured_edit_outside_affected_files_is_rejected_without_commit_or_push(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    ollama = FakeOllama([structured_proposal(path="OTHER.md")])

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), ollama, artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_stage == "structured_edit_apply"
    assert report.failure_reason == "outside_task_scope"
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert git.committed is False
    assert git.pushed is False
    assert "structured_edit_error.json" in report.patch_artifacts


def test_successful_structured_edit_pushes_only_when_enabled(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cfg = config(repo).model_copy(update={"push_agent_branches_enabled": True})
    git = FakeGitTool()
    ollama = FakeOllama([structured_proposal()])

    report = ImplementationAgent(cfg, git, FileTool(repo, cfg), ollama).implement(docs_task())

    assert report.status == ReportStatus.PASSED
    assert git.committed is True
    assert git.pushed is True
    assert report.pushed is True


def test_patch_proposal_remains_for_non_docs_tasks(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=0)
    ollama = FakeOllama([proposal()])
    non_docs = task().model_copy(update={"task_type": TaskType.BUGFIX, "affected_files": []})

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(non_docs)

    assert report.status == ReportStatus.PASSED
    assert report.implementation_mode == "patch"
    assert ollama.response_models == [PatchProposal]


def test_corrupt_patch_for_docs_task_can_fallback_to_structured_edit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    ollama = FakeOllama([proposal(), structured_proposal()])
    docs = docs_task()
    real_file_tool = FileTool(repo, config(repo))

    class CorruptPatchThenStructuredFileTool(FileTool):
        def apply_patch(self, proposal: PatchProposal) -> DiffStats:
            raise PatchApplyError(
                command=["git", "apply", "--check", "--whitespace=nowarn", "-"],
                stderr="error: corrupt patch at line 32\n",
                patch=proposal.patch,
                check=True,
            )

        def apply_structured_edits(self, proposal: StructuredEditProposal) -> DiffStats:
            return real_file_tool.apply_structured_edits(proposal)

        def read_file(self, relative_path: str, *, max_bytes: int = 200_000) -> str:
            return real_file_tool.read_file(relative_path, max_bytes=max_bytes)

    agent = ImplementationAgent(config(repo), git, CorruptPatchThenStructuredFileTool(repo, config(repo)), ollama, artifacts=store)

    docs_checks = iter([False, True])
    agent._is_docs_task = lambda ignored: next(docs_checks)  # type: ignore[method-assign]
    report = agent.implement(docs)

    assert report.status == ReportStatus.PASSED
    assert report.implementation_mode == "structured_edit"
    assert report.fallback_attempted is True
    assert report.fallback_succeeded is True
    assert report.fallback_reason == "corrupt_patch"
    apply_report = json.loads(read_artifact(store, "structured_edit_apply_report.json"))
    assert apply_report["fallback_from"] == "PatchProposal"
    assert apply_report["fallback_to"] == "StructuredEditProposal"


def test_malformed_patch_writes_debug_artifacts_and_failed_report(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=2)
    ollama = FakeOllama([proposal(), proposal("repair")])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(patch_task())

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


def test_validation_error_writes_artifact_and_repair_prompt_gets_line_context(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = ValidatingFakeFileTool()
    ollama = FakeOllama([proposal_with_patch(INVALID_HEADING_PATCH), proposal_with_patch(INVALID_RULE_PATCH, "repair")])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(patch_task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_stage == "patch_validation"
    assert report.failure_reason == "missing_diff_prefix_in_hunk"
    assert report.retry_attempted is True
    assert report.retry_succeeded is False
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert file_tool.apply_calls == 0
    assert git.committed is False
    assert git.pushed is False
    assert "patch_validation_error.json" in report.patch_artifacts
    assert "repair_patch_validation_error.json" in report.patch_artifacts
    assert "line 6" in report.errors[0]
    assert "## Security & Deployment Assumptions" in report.errors[0]
    validation = json.loads(read_artifact(store, "patch_validation_error.json"))
    assert validation == {
        "line_number": 6,
        "offending_line": "## Security & Deployment Assumptions",
        "reason": "missing_diff_prefix_in_hunk",
    }
    repair_prompt = ollama.prompts[1]
    assert '"line_number": 6' in repair_prompt
    assert '"offending_line": "## Security & Deployment Assumptions"' in repair_prompt
    assert "Every line inside a unified diff hunk must start with space, +, -, or backslash." in repair_prompt
    assert "Markdown lines that should be added must start with +" in repair_prompt
    assert "Do not wrap the diff in Markdown fences." in repair_prompt
    assert "Do not include explanations outside JSON." in repair_prompt


def test_corrupt_patch_repair_success_continues_to_commit(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    file_tool = FakeFileTool(fail_times=1)
    ollama = FakeOllama([proposal(), proposal("repair")])

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(patch_task())

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

    report = ImplementationAgent(config(tmp_path), git, file_tool, ollama, artifacts=store).implement(patch_task())

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
