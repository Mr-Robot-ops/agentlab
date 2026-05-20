from pathlib import Path

import pytest
from pydantic import ValidationError

from agentlab.config import AppConfig
from agentlab.models import AgentTask, FileEdit, GateDecision, PatchProposal, ReviewReport, StructuredEditProposal, TaskType, Verdict


def test_task_rejects_unsafe_id() -> None:
    with pytest.raises(ValidationError):
        AgentTask(id="../bad", title="Bad")


def test_task_accepts_structured_fields() -> None:
    task = AgentTask(
        id="safe-1",
        title="Safe",
        task_type=TaskType.BUGFIX,
        acceptance_criteria=["passes tests"],
        affected_files=["src/app.py"],
        approved=True,
    )
    assert task.id == "safe-1"
    assert task.task_type == TaskType.BUGFIX


def test_patch_proposal_requires_rollback() -> None:
    with pytest.raises(ValidationError):
        PatchProposal(task_id="safe-1", summary="x", patch="diff --git a/x b/x\n", affected_files=["x"])


def test_review_report_rejects_unknown_verdict() -> None:
    with pytest.raises(ValidationError):
        ReviewReport(reviewer="quality", verdict="maybe", summary="nope")  # type: ignore[arg-type]


def test_gate_decision_serializes_to_json_values() -> None:
    decision = GateDecision(
        allowed=False,
        mode="merge_request",
        verdict="blocked",
        risk_score=99,
        blockers=["risk"],
    )
    assert decision.model_dump(mode="json")["verdict"] == "blocked"
    assert Verdict.APPROVED.value == "approved"


def test_config_keeps_dangerous_defaults_disabled() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
    )

    assert config.auto_merge_enabled is False
    assert config.direct_main_push_enabled is False
    assert config.push_agent_branches_enabled is False


def test_file_edit_accepts_path_and_operation_aliases() -> None:
    proposal = StructuredEditProposal(
        task_id="t1",
        summary="docs",
        edits=[{"file_path": "README.md", "tool": "replace_text", "old_text": "a", "new_text": "b"}],
        rollback="revert",
    )

    edit = proposal.edits[0]
    assert edit.path == "README.md"
    assert edit.operation == "replace_text"
    assert proposal.model_dump(mode="json")["edits"][0] == {
        "path": "README.md",
        "operation": "replace_text",
        "content": None,
        "old_text": "a",
        "new_text": "b",
        "anchor": None,
    }


@pytest.mark.parametrize("alias", ["filepath", "filename", "file"])
def test_file_edit_accepts_path_alias_variants(alias: str) -> None:
    edit = FileEdit.model_validate({alias: "README.md", "operation": "append", "content": "\nmore\n"})

    assert edit.path == "README.md"
    assert edit.operation == "append_to_file"


@pytest.mark.parametrize("alias", ["op", "action"])
def test_file_edit_accepts_operation_alias_variants(alias: str) -> None:
    edit = FileEdit.model_validate({"path": "README.md", alias: "write_file", "content": "new\n"})

    assert edit.operation == "replace_file"


def test_file_edit_allows_matching_path_aliases() -> None:
    edit = FileEdit.model_validate({"path": "README.md", "file_path": "README.md", "operation": "append", "content": "x"})

    assert edit.path == "README.md"
    assert edit.operation == "append_to_file"


def test_file_edit_rejects_conflicting_path_aliases() -> None:
    with pytest.raises(ValidationError, match="conflicting path aliases"):
        FileEdit.model_validate({"path": "README.md", "file_path": "docs/README.md", "operation": "append", "content": "x"})


def test_file_edit_allows_matching_operation_aliases() -> None:
    edit = FileEdit.model_validate({"path": "README.md", "operation": "replace_text", "tool": "replace", "old_text": "a", "new_text": "b"})

    assert edit.operation == "replace_text"


def test_file_edit_rejects_conflicting_operation_aliases() -> None:
    with pytest.raises(ValidationError, match="conflicting operation aliases"):
        FileEdit.model_validate({"path": "README.md", "operation": "replace_file", "tool": "append", "content": "x"})


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("replace", "replace_text"),
        ("replaceText", "replace_text"),
        ("text_replace", "replace_text"),
        ("append", "append_to_file"),
        ("append_file", "append_to_file"),
        ("write_file", "replace_file"),
        ("overwrite_file", "replace_file"),
        ("insertBefore", "insert_before"),
        ("before", "insert_before"),
        ("insert_before_anchor", "insert_before"),
        ("insertAfter", "insert_after"),
        ("after", "insert_after"),
        ("insert_after_anchor", "insert_after"),
    ],
)
def test_file_edit_normalizes_operation_aliases(raw: str, normalized: str) -> None:
    payload = {"path": "README.md", "operation": raw, "content": "x", "old_text": "a", "new_text": "b", "anchor": "## Heading"}

    assert FileEdit.model_validate(payload).operation == normalized


def test_file_edit_rejects_unknown_extra_alias() -> None:
    with pytest.raises(ValidationError):
        FileEdit.model_validate({"path": "README.md", "operation": "append", "target": "README.md", "content": "x"})


def test_file_edit_rejects_unknown_operation() -> None:
    with pytest.raises(ValidationError):
        FileEdit.model_validate({"path": "README.md", "operation": "rewrite_section", "content": "x"})
