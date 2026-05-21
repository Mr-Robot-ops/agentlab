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
from agentlab.tools.ollama_client import OllamaSchemaValidationError


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
        self.created_branch: str | None = None
        self.pushed_branch: str | None = None
        self.checked_out: str | None = None

    def create_branch(self, branch: str, base: str) -> CommandResult:
        self.created_branch = branch
        return CommandResult(command=f"git checkout -B {branch} {base}", cwd=".", exit_code=0)

    def checkout(self, branch: str) -> CommandResult:
        self.checked_out = branch
        return CommandResult(command=f"git checkout {branch}", cwd=".", exit_code=0)

    def commit(self, message: str) -> str:
        self.committed = True
        return "abc123"

    def push(self, branch: str) -> CommandResult:
        self.pushed = True
        self.pushed_branch = branch
        return CommandResult(command=f"git push origin {branch}", cwd=".", exit_code=0)


class MissingIdentityGitTool(FakeGitTool):
    def commit(self, message: str) -> str:
        raise ToolError(
            "Author identity unknown\n\n"
            "*** Please tell me who you are.\n\n"
            "fatal: unable to auto-detect email address"
        )


class NonFastForwardGitTool(FakeGitTool):
    def push(self, branch: str) -> CommandResult:
        self.pushed = True
        self.pushed_branch = branch
        return CommandResult(
            command=f"git push origin {branch}",
            cwd=".",
            exit_code=1,
            stderr=(
                "! [rejected] agent/document-privileged-container-boundaries -> "
                "agent/document-privileged-container-boundaries (non-fast-forward)\n"
            ),
        )


class GenericPushFailGitTool(FakeGitTool):
    def push(self, branch: str) -> CommandResult:
        self.pushed = True
        self.pushed_branch = branch
        return CommandResult(command=f"git push origin {branch}", cwd=".", exit_code=1, stderr="remote: unavailable\n")


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


class RawStructuredOllama:
    def __init__(self, raw_response: str) -> None:
        self.raw_response = raw_response
        self.calls = 0
        self.response_models: list[type[Any]] = []

    def chat_json_with_raw(self, **kwargs: Any) -> tuple[Any, str]:
        self.calls += 1
        self.response_models.append(kwargs["response_model"])
        model = kwargs["response_model"]
        return model.model_validate_json(self.raw_response), self.raw_response


class SchemaErrorOllama:
    def __init__(self, raw_response: str, validation_error: str = "validation failed") -> None:
        self.raw_response = raw_response
        self.validation_error = validation_error

    def chat_json_with_raw(self, **kwargs: Any) -> tuple[Any, str]:
        raise OllamaSchemaValidationError(
            model_name=kwargs["response_model"].__name__,
            validation_error=self.validation_error,
            raw_response=self.raw_response,
        )


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


def commit_all(repo: Path, message: str = "initial") -> None:
    subprocess.run(["git", "config", "user.email", "agentlab@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "AgentLab"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def test_structured_insert_before_inserts_before_unique_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, content="# AgentLab\n\n## Quick Start\nRun it.\n")
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="insert",
        edits=[FileEdit(path="README.md", operation="insert_before", anchor="## Quick Start", content="## Security\nSafe.\n\n")],
        rollback="Remove inserted section.",
    )

    file_tool_for(repo).apply_structured_edits(proposal)

    assert "## Security\nSafe.\n\n## Quick Start" in (repo / "README.md").read_text(encoding="utf-8")


def test_structured_insert_after_inserts_after_unique_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, content="# AgentLab\nOpen **http://localhost:8080** — default API key is `admin123`.\n")
    anchor = "Open **http://localhost:8080** — default API key is `admin123`."
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="insert",
        edits=[FileEdit(path="README.md", operation="insert_after", anchor=anchor, content="\n\n### Deployment Assumptions\nLocal only.\n")],
        rollback="Remove inserted section.",
    )

    file_tool_for(repo).apply_structured_edits(proposal)

    assert f"{anchor}\n\n### Deployment Assumptions" in (repo / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["insert_before", "insert_after"])
def test_structured_insert_fails_when_anchor_missing(tmp_path: Path, operation: str) -> None:
    repo = init_repo(tmp_path, content="# AgentLab\n")
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="insert",
        edits=[FileEdit(path="README.md", operation=operation, anchor="## Missing", content="Text\n")],
        rollback="Remove inserted section.",
    )

    with pytest.raises(ToolError, match="anchor not found"):
        file_tool_for(repo).apply_structured_edits(proposal)


@pytest.mark.parametrize("operation", ["insert_before", "insert_after"])
def test_structured_insert_fails_when_anchor_repeats(tmp_path: Path, operation: str) -> None:
    repo = init_repo(tmp_path, content="## Repeat\nBody\n## Repeat\n")
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="insert",
        edits=[FileEdit(path="README.md", operation=operation, anchor="## Repeat", content="Text\n")],
        rollback="Remove inserted section.",
    )

    with pytest.raises(ToolError, match="multiple times"):
        file_tool_for(repo).apply_structured_edits(proposal)


def test_structured_insert_touching_protected_path_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "protected.md").write_text("## Anchor\n", encoding="utf-8")
    cfg = config(repo).model_copy(update={"protected_paths": ["docs"]})
    proposal = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="insert",
        edits=[FileEdit(path="docs/protected.md", operation="insert_before", anchor="## Anchor", content="Text\n")],
        rollback="Remove inserted section.",
    )

    with pytest.raises(ToolError, match="protected"):
        FileTool(repo, cfg).apply_structured_edits(proposal)


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


README_STRUCTURE = """# Demo

## Project Structure

```text
.
+-- rust-backend/
|   +-- src/
|       +-- routes/
|           +-- health.rs
|           +-- users.rs
+-- web/
    +-- src/
        +-- App.tsx
```

## Usage
Run it.
"""


def write_project_structure_files(repo: Path, *, include_dist: bool = False) -> None:
    for path in [
        "rust-backend/src/routes/health.rs",
        "rust-backend/src/routes/users.rs",
        "web/src/App.tsx",
        ".github/workflows/ci.yml",
    ]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("content\n", encoding="utf-8")
    if include_dist:
        target = repo / "web/dist/bundle.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("built\n", encoding="utf-8")


def structure_task(description: str = "Update the README Project Structure to match actual files.") -> AgentTask:
    return docs_task().model_copy(update={"description": description})


def structure_replace_proposal(old_text: str, new_text: str) -> StructuredEditProposal:
    return structured_proposal(old_text=old_text, new_text=new_text)


def test_readme_project_structure_update_keeps_existing_files_and_writes_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, README_STRUCTURE)
    write_project_structure_files(repo)
    store = ArtifactStore(tmp_path / "run", "run")
    proposed = README_STRUCTURE.replace(
        ".\n+-- rust-backend/",
        ".\n+-- .github/\n|   +-- workflows/\n|       +-- ci.yml\n+-- rust-backend/",
    )

    report = ImplementationAgent(
        config(repo),
        FakeGitTool(),
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal(README_STRUCTURE, proposed)]),
        artifacts=store,
        run_id="run",
    ).implement(structure_task())

    assert report.status == ReportStatus.PASSED
    assert "project_structure_evidence.json" in report.patch_artifacts
    evidence = json.loads(read_artifact(store, "project_structure_evidence.json"))
    assert evidence["validation_status"] == "passed"
    assert evidence["removed_existing_entries"] == []
    assert evidence["added_entries"] == [".github/workflows/ci.yml"]
    assert "rust-backend/src/routes/users.rs" in evidence["collected_files"]


def test_readme_project_structure_prompt_includes_real_file_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, README_STRUCTURE)
    write_project_structure_files(repo)
    proposed = README_STRUCTURE.replace("Run it.", "Run it locally.")
    ollama = FakeOllama([structure_replace_proposal(README_STRUCTURE, proposed)])

    report = ImplementationAgent(
        config(repo),
        FakeGitTool(),
        FileTool(repo, config(repo)),
        ollama,
    ).implement(structure_task())

    assert report.status == ReportStatus.PASSED
    assert "project_structure_evidence" in ollama.prompts[0]
    assert "find .github rust-backend web -maxdepth 4 -type f | sort" in ollama.prompts[0]
    assert "rust-backend/src/routes/users.rs" in ollama.prompts[0]


def test_readme_project_structure_removing_existing_route_file_is_blocked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, README_STRUCTURE)
    write_project_structure_files(repo)
    store = ArtifactStore(tmp_path / "run", "run")
    proposed = README_STRUCTURE.replace("|           +-- users.rs\n", "")
    git = FakeGitTool()

    report = ImplementationAgent(
        config(repo),
        git,
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal(README_STRUCTURE, proposed)]),
        artifacts=store,
        run_id="run",
    ).implement(structure_task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_reason == "project_structure_validation_failed"
    assert git.committed is False
    assert (repo / "README.md").read_text(encoding="utf-8") == README_STRUCTURE
    evidence = json.loads(read_artifact(store, "project_structure_evidence.json"))
    assert evidence["validation_status"] == "blocked"
    assert evidence["removed_existing_entries"] == ["rust-backend/src/routes/users.rs"]
    error = json.loads(read_artifact(store, "structured_edit_error.json"))
    assert error["removed_existing_entries"] == ["rust-backend/src/routes/users.rs"]


def test_readme_project_structure_ignores_web_dist_files_by_default(tmp_path: Path) -> None:
    readme = README_STRUCTURE.replace(
        "+-- web/\n    +-- src/",
        "+-- web/\n    +-- dist/\n    |   +-- bundle.js\n    +-- src/",
    )
    repo = init_repo(tmp_path, readme)
    write_project_structure_files(repo, include_dist=True)
    store = ArtifactStore(tmp_path / "run", "run")
    proposed = readme.replace("    +-- dist/\n    |   +-- bundle.js\n", "")

    report = ImplementationAgent(
        config(repo),
        FakeGitTool(),
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal(readme, proposed)]),
        artifacts=store,
        run_id="run",
    ).implement(structure_task())

    assert report.status == ReportStatus.PASSED
    evidence = json.loads(read_artifact(store, "project_structure_evidence.json"))
    assert evidence["removed_existing_entries"] == []
    assert "web/dist/bundle.js" in evidence["ignored_files"]


def test_readme_project_structure_compact_summary_requires_explicit_request(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, README_STRUCTURE)
    write_project_structure_files(repo)
    compact = """# Demo

## Project Structure

This is a compact summary, not a complete file tree.

```text
.
+-- rust-backend/
+-- web/
```

## Usage
Run it.
"""

    blocked_store = ArtifactStore(tmp_path / "blocked-run", "blocked")
    blocked = ImplementationAgent(
        config(repo),
        FakeGitTool(),
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal(README_STRUCTURE, compact)]),
        artifacts=blocked_store,
        run_id="blocked",
    ).implement(structure_task("Update the README Project Structure to match actual files."))

    assert blocked.status == ReportStatus.FAILED
    assert blocked.failure_reason == "project_structure_validation_failed"

    allowed_store = ArtifactStore(tmp_path / "allowed-run", "allowed")
    allowed = ImplementationAgent(
        config(repo),
        FakeGitTool(),
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal(README_STRUCTURE, compact)]),
        artifacts=allowed_store,
        run_id="allowed",
    ).implement(structure_task("Create a compact summary of the README Project Structure."))

    assert allowed.status == ReportStatus.PASSED
    evidence = json.loads(read_artifact(allowed_store, "project_structure_evidence.json"))
    assert evidence["validation_status"] == "passed"
    assert "rust-backend/src/routes/users.rs" in evidence["removed_existing_entries"]


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


def test_docs_prompt_prefers_insert_operations(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    ollama = FakeOllama([structured_proposal()])

    ImplementationAgent(config(repo), FakeGitTool(), FileTool(repo, config(repo)), ollama).implement(docs_task())

    prompt = ollama.prompts[0]
    assert "use insert_before or insert_after for new sections" in prompt
    assert '"operation": "insert_before"' in prompt
    assert '"operation": "insert_after"' in prompt
    assert "old_text must be copied exactly from file_snippets" in prompt


def test_structured_edit_accepts_file_path_tool_aliases_and_writes_normalized_artifact(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    raw = json.dumps(
        {
            "task_id": "document-privileged-container-boundaries",
            "summary": "structured docs",
            "edits": [
                {
                    "file_path": "README.md",
                    "tool": "replace",
                    "old_text": "# AgentLab\n",
                    "new_text": "# AgentLab\n\nMore docs\n",
                }
            ],
            "expected_tests": [],
            "risk_score": 1,
            "rollback": "Revert README.md changes.",
            "metadata": {},
        }
    )

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), RawStructuredOllama(raw), artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.PASSED
    assert report.implementation_mode == "structured_edit"
    assert git.committed is True
    proposal_artifact = json.loads(read_artifact(store, "structured_edit_proposal.json"))
    assert proposal_artifact["edits"][0]["path"] == "README.md"
    assert proposal_artifact["edits"][0]["operation"] == "replace_text"
    assert "file_path" not in proposal_artifact["edits"][0]
    assert "tool" not in proposal_artifact["edits"][0]


def test_structured_edit_schema_error_writes_artifacts_and_does_not_commit_or_push(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    raw = json.dumps(
        {
            "task_id": "document-privileged-container-boundaries",
            "summary": "bad",
            "edits": [{"file_path": "README.md", "tool": "replace_text", "new_text": "missing old"}],
            "rollback": "revert",
        }
    )

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), SchemaErrorOllama(raw), artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.implementation_mode == "structured_edit"
    assert report.failure_stage == "structured_edit_schema_validation"
    assert report.failure_reason == "schema_validation_failed"
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert git.committed is False
    assert git.pushed is False
    assert "structured_edit_raw_response.json" in report.patch_artifacts
    assert "structured_edit_schema_error.json" in report.patch_artifacts
    assert "StructuredEditProposal schema validation failed: edits[0] used file_path/tool; expected path/operation" in report.errors
    schema_error = json.loads(read_artifact(store, "structured_edit_schema_error.json"))
    assert schema_error["normalized_attempt"]["edits"][0]["path"] == "README.md"
    assert schema_error["normalized_attempt"]["edits"][0]["operation"] == "replace_text"
    assert schema_error["expected_schema_hint"]["edit_path_field"] == "path"


def test_structured_edit_error_artifact_includes_context_hashes_and_repr(tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path,
        content="# AgentLab\nOpen **http://localhost:8080** — default API key is `admin123`.\n## Quick Start\n",
    )
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    literal_backslash = "Open **http://localhost:8080** \\u2014 default API key is `admin123`."
    bad = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="bad",
        edits=[FileEdit(path="README.md", operation="replace_text", old_text=literal_backslash, new_text="replacement")],
        rollback="Revert README.md changes.",
    )

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), FakeOllama([bad]), artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    error = json.loads(read_artifact(store, "structured_edit_error.json"))
    assert error["failing_edit_index"] == 0
    assert error["path"] == "README.md"
    assert error["operation"] == "replace_text"
    assert error["old_text_excerpt"] == literal_backslash
    assert "\\\\u2014" in error["old_text_repr_excerpt"]
    assert error["old_text_sha256"]
    assert error["file_sha256"]
    assert error["target_file_exists"] is True
    assert error["target_file_size"] > 0
    assert error["candidate_contexts"]
    assert "—" in error["candidate_contexts"][0]
    assert error["no_changes_committed"] is True
    assert error["no_branch_pushed"] is True


def test_structured_repair_succeeds_after_anchor_correction(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, content="# AgentLab\n\n## Quick Start\nRun it.\n")
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    bad = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="bad",
        edits=[FileEdit(path="README.md", operation="insert_before", anchor="## Missing", content="## Security\nSafe.\n\n")],
        rollback="Revert README.md changes.",
    )
    fixed = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="fixed",
        edits=[FileEdit(path="README.md", operation="insert_before", anchor="## Quick Start", content="## Security\nSafe.\n\n")],
        rollback="Revert README.md changes.",
    )

    ollama = FakeOllama([bad, fixed])
    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), ollama, artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.PASSED
    assert report.retry_attempted is True
    assert report.retry_succeeded is True
    assert git.committed is True
    assert "structured_edit_repair_raw_response.json" in report.patch_artifacts
    assert "structured_edit_repair_proposal.json" in report.patch_artifacts
    assert "structured_edit_repair_apply_report.json" in report.patch_artifacts
    repair_prompt = ollama.prompts[1]
    assert "candidate_contexts" in repair_prompt
    assert "only repair anchors or old_text" in repair_prompt
    assert "## Security\nSafe.\n\n## Quick Start" in (repo / "README.md").read_text(encoding="utf-8")


def test_structured_repair_failure_does_not_commit_or_push(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, content="# AgentLab\n\n## Quick Start\nRun it.\n")
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()
    bad = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="bad",
        edits=[FileEdit(path="README.md", operation="insert_before", anchor="## Missing", content="## Security\nSafe.\n\n")],
        rollback="Revert README.md changes.",
    )
    still_bad = StructuredEditProposal(
        task_id="document-privileged-container-boundaries",
        summary="still bad",
        edits=[FileEdit(path="README.md", operation="insert_after", anchor="## Still Missing", content="More\n")],
        rollback="Revert README.md changes.",
    )

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), FakeOllama([bad, still_bad]), artifacts=store).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.retry_attempted is True
    assert report.retry_succeeded is False
    assert report.failure_stage == "structured_edit_apply"
    assert report.failure_reason == "anchor_not_found"
    assert git.committed is False
    assert git.pushed is False
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert "structured_edit_repair_error.json" in report.patch_artifacts


def test_commit_missing_identity_sets_git_commit_failure_reason(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    git = MissingIdentityGitTool()

    report = ImplementationAgent(config(repo), git, FileTool(repo, config(repo)), FakeOllama([structured_proposal()])).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_stage == "git_commit"
    assert report.failure_reason == "git_author_identity_missing"
    assert report.commit_sha is None
    assert report.pushed is False
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert "Author identity unknown" in report.errors[0]


def test_normalized_structured_edit_can_push_when_enabled(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cfg = config(repo).model_copy(update={"push_agent_branches_enabled": True})
    git = FakeGitTool()
    raw = json.dumps(
        {
            "task_id": "document-privileged-container-boundaries",
            "summary": "structured docs",
            "edits": [
                {
                    "file_path": "README.md",
                    "tool": "replaceText",
                    "old_text": "# AgentLab\n",
                    "new_text": "# AgentLab\n\nMore docs\n",
                }
            ],
            "rollback": "Revert README.md changes.",
        }
    )

    report = ImplementationAgent(
        cfg,
        git,
        FileTool(repo, cfg),
        RawStructuredOllama(raw),
        run_id="cb9b06c1ab70433ea9bfce1602691d3b",
    ).implement(docs_task())

    assert report.status == ReportStatus.PASSED
    assert git.committed is True
    assert git.pushed is True
    assert report.pushed is True
    assert report.branch == "agent/document-privileged-container-boundaries-cb9b06c1"
    assert git.created_branch == report.branch
    assert git.pushed_branch == report.branch


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


def test_propose_on_branch_writes_stable_artifacts_without_commit_push_or_dirty_tree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_all(repo)
    store = ArtifactStore(tmp_path / "run", "run")
    cfg = config(repo).model_copy(update={"push_agent_branches_enabled": True})
    git = FakeGitTool()

    report = ImplementationAgent(
        cfg,
        git,
        FileTool(repo, cfg),
        FakeOllama([structured_proposal()]),
        artifacts=store,
    ).propose_on_branch(docs_task(), "agent/docs")

    assert report.status == ReportStatus.PASSED
    assert report.applied is False
    assert report.pushed is False
    assert report.commit_sha is None
    assert report.no_changes_committed is True
    assert report.no_branch_pushed is True
    assert git.committed is False
    assert git.pushed is False
    assert git.checked_out == "agent/docs"
    assert "structured_proposal.json" in report.patch_artifacts
    assert "proposed.diff" in report.patch_artifacts
    assert "structured_proposal_report.json" in report.patch_artifacts
    assert "+More docs" in read_artifact(store, "proposed.diff")
    proposal_report = json.loads(read_artifact(store, "structured_proposal_report.json"))
    assert proposal_report["proposal_artifacts"] == ["structured_proposal.json", "proposed.diff", "structured_proposal_report.json"]
    assert proposal_report["sensitive_content_detected"] is False
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_propose_on_branch_project_structure_evidence_blocks_removed_existing_files(tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path,
        content=(
            "# AgentLab\n\n## Project Structure\n\n```text\n.\n+-- rust-backend/\n"
            "    +-- src/\n        +-- routes/\n            +-- health.rs\n```\n"
        ),
    )
    (repo / "rust-backend/src/routes").mkdir(parents=True)
    (repo / "rust-backend/src/routes/health.rs").write_text("health\n", encoding="utf-8")
    commit_all(repo)
    proposed = (repo / "README.md").read_text(encoding="utf-8").replace("            +-- health.rs\n", "")
    store = ArtifactStore(tmp_path / "run", "run")
    git = FakeGitTool()

    report = ImplementationAgent(
        config(repo),
        git,
        FileTool(repo, config(repo)),
        FakeOllama([structure_replace_proposal((repo / "README.md").read_text(encoding="utf-8"), proposed)]),
        artifacts=store,
    ).propose_on_branch(docs_task(), "agent/docs")

    assert report.status == ReportStatus.FAILED
    assert report.failure_reason == "project_structure_validation_failed"
    assert git.committed is False
    assert git.pushed is False
    evidence = json.loads(read_artifact(store, "project_structure_evidence.json"))
    assert evidence["validation_status"] == "blocked"
    assert evidence["removed_existing_entries"] == ["rust-backend/src/routes/health.rs"]


def test_repeated_runs_use_distinct_agent_branches(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_repo = init_repo(first_root)
    second_repo = init_repo(second_root)
    first_git = FakeGitTool()
    second_git = FakeGitTool()

    first = ImplementationAgent(
        config(first_repo),
        first_git,
        FileTool(first_repo, config(first_repo)),
        FakeOllama([structured_proposal()]),
        run_id="aaaaaaaa11111111",
    ).implement(docs_task())
    second = ImplementationAgent(
        config(second_repo),
        second_git,
        FileTool(second_repo, config(second_repo)),
        FakeOllama([structured_proposal()]),
        run_id="bbbbbbbb22222222",
    ).implement(docs_task())

    assert first.branch != second.branch
    assert first.branch.endswith("-aaaaaaaa")
    assert second.branch.endswith("-bbbbbbbb")


def test_push_non_fast_forward_preserves_local_commit_sha(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cfg = config(repo).model_copy(update={"push_agent_branches_enabled": True})
    git = NonFastForwardGitTool()

    report = ImplementationAgent(
        cfg,
        git,
        FileTool(repo, cfg),
        FakeOllama([structured_proposal()]),
        run_id="cb9b06c1ab70433ea9bfce1602691d3b",
    ).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.branch == "agent/document-privileged-container-boundaries-cb9b06c1"
    assert report.failure_stage == "git_push"
    assert report.failure_reason == "non_fast_forward"
    assert report.commit_sha == "abc123"
    assert report.pushed is False
    assert report.no_changes_committed is False
    assert report.no_branch_pushed is True
    assert "non-fast-forward" in report.errors[0]


def test_generic_push_failure_is_classified(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cfg = config(repo).model_copy(update={"push_agent_branches_enabled": True})
    git = GenericPushFailGitTool()

    report = ImplementationAgent(
        cfg,
        git,
        FileTool(repo, cfg),
        FakeOllama([structured_proposal()]),
        run_id="cccccccc33333333",
    ).implement(docs_task())

    assert report.status == ReportStatus.FAILED
    assert report.failure_stage == "git_push"
    assert report.failure_reason == "push_failed"
    assert report.commit_sha == "abc123"
    assert report.no_changes_committed is False
    assert report.no_branch_pushed is True


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
