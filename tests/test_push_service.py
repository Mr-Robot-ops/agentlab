from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import (
    AgentTask,
    BuildSecurityReport,
    CommandResult,
    DiffStats,
    GateDecision,
    ImplementationReport,
    ReportStatus,
    ReviewReport,
    Verdict,
)
from agentlab.models import TestReport as AgentTestReport
from agentlab.services.push_service import PushService


class FakeGitTool:
    def __init__(
        self,
        *,
        status_sequence: list[str] | None = None,
        cherry_pick_ok: bool = True,
        final_diff: DiffStats | None = None,
    ) -> None:
        self.status_sequence = status_sequence or ["", ""]
        self.cherry_pick_ok = cherry_pick_ok
        self.final_diff = final_diff or DiffStats(changed_files=["src/app.py"], added_lines=1)
        self.calls: list[tuple[str, object]] = []

    def status_porcelain(self) -> str:
        self.calls.append(("status_porcelain", None))
        if self.status_sequence:
            return self.status_sequence.pop(0)
        return ""

    def checkout(self, branch: str) -> CommandResult:
        self.calls.append(("checkout", branch))
        return CommandResult(command="git checkout", cwd=".", exit_code=0)

    def pull_ff_only(self, *, branch: str) -> CommandResult:
        self.calls.append(("pull_ff_only", branch))
        return CommandResult(command="git pull --ff-only", cwd=".", exit_code=0)

    def cherry_pick(self, commit_sha: str, *, no_commit: bool = False, allow_default_branch: bool = False) -> CommandResult:
        self.calls.append(("cherry_pick", (commit_sha, no_commit, allow_default_branch)))
        return CommandResult(
            command="git cherry-pick",
            cwd=".",
            exit_code=0 if self.cherry_pick_ok else 1,
            stderr="" if self.cherry_pick_ok else "conflict",
        )

    def diff_stats(self, base: str, protected_paths: list[str]) -> DiffStats:
        self.calls.append(("diff_stats", (base, tuple(protected_paths))))
        return self.final_diff

    def commit_direct_main(self, message: str) -> str:
        self.calls.append(("commit_direct_main", message))
        return "commit123"

    def push_default_branch(self) -> CommandResult:
        self.calls.append(("push_default_branch", None))
        return CommandResult(command="git push origin main", cwd=".", exit_code=0)


class FakeTestTool:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.commands: list[str] = []

    def run_command(self, command: str) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command=command, cwd=".", exit_code=0 if self.ok else 1, stderr="" if self.ok else "failed")


def config(**overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": Path("."),
        "workspace_root": Path(".runs"),
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def inputs(diff_stats: DiffStats | None = None) -> dict[str, object]:
    return {
        "task": AgentTask(id="t1", title="Task", approved=True),
        "implementation": ImplementationReport(
            task_id="t1",
            branch="agent/t1",
            status=ReportStatus.PASSED,
            commit_sha="abc123",
        ),
        "gate": GateDecision(allowed=True, mode="direct_main_push", verdict="allowed", risk_score=5),
        "diff_stats": diff_stats or DiffStats(changed_files=["src/app.py"], added_lines=1),
        "functional_tests": AgentTestReport(status=ReportStatus.PASSED, passed=True),
        "build_security": BuildSecurityReport(status=ReportStatus.PASSED, passed=True),
        "quality_review": ReviewReport(reviewer="quality", verdict=Verdict.APPROVED, summary="ok"),
        "security_review": ReviewReport(reviewer="security_architecture", verdict=Verdict.APPROVED, summary="ok"),
        "rollback_plan": "revert commit",
        "audit_id": "run-1",
    }


def test_push_service_refuses_when_direct_push_disabled() -> None:
    result = PushService(config(), FakeGitTool(), FakeTestTool()).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert "direct_main_push_enabled is false" in result.errors


def test_push_service_refuses_protected_paths() -> None:
    result = PushService(
        config(direct_main_push_enabled=True),
        FakeGitTool(),
        FakeTestTool(),
    ).push_direct_main(
        **inputs(DiffStats(changed_files=["infra/prod/main.tf"], touched_protected_paths=["infra/prod/main.tf"]))
    )  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert any("protected paths touched" in error for error in result.errors)


def test_push_service_refuses_secrets_touched() -> None:
    result = PushService(
        config(direct_main_push_enabled=True),
        FakeGitTool(),
        FakeTestTool(),
    ).push_direct_main(
        **inputs(DiffStats(changed_files=["secrets/api.env"], secrets_touched=True))
    )  # type: ignore[arg-type]

    assert result.status == ReportStatus.SKIPPED
    assert "secrets touched" in result.errors


def test_push_service_happy_path_pushes_default_branch() -> None:
    git = FakeGitTool(status_sequence=["", ""])
    test_tool = FakeTestTool()

    result = PushService(
        config(direct_main_push_enabled=True, required_test_commands=["python -m pytest"]),
        git,
        test_tool,
    ).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.PASSED
    assert result.pushed is True
    assert result.local_commit_created is True
    assert result.commit_sha == "commit123"
    assert [call[0] for call in git.calls] == [
        "status_porcelain",
        "checkout",
        "pull_ff_only",
        "cherry_pick",
        "diff_stats",
        "commit_direct_main",
        "status_porcelain",
        "push_default_branch",
    ]
    assert git.calls[1] == ("checkout", "main")
    assert git.calls[2] == ("pull_ff_only", "main")
    assert ("cherry_pick", ("abc123", True, True)) in git.calls
    assert git.calls[4] == ("diff_stats", ("HEAD", ()))
    assert git.calls[5][0] == "commit_direct_main"
    assert ("push_default_branch", None) in git.calls
    assert test_tool.commands == ["python -m pytest"]


def test_push_service_failed_required_test_does_not_push() -> None:
    git = FakeGitTool(status_sequence=["", ""])

    result = PushService(
        config(direct_main_push_enabled=True, required_test_commands=["python -m pytest"]),
        git,
        FakeTestTool(ok=False),
    ).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert result.local_commit_created is True
    assert result.pushed is False
    assert ("push_default_branch", None) not in git.calls
    assert result.recommended_recovery


def test_push_service_dirty_workspace_before_push_fails() -> None:
    git = FakeGitTool(status_sequence=[" M file.py"])

    result = PushService(config(direct_main_push_enabled=True), git, FakeTestTool()).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert "workspace is dirty before direct-main push" in result.errors
    assert all(call[0] != "checkout" for call in git.calls)


def test_push_service_dirty_workspace_after_tests_fails_without_push() -> None:
    git = FakeGitTool(status_sequence=["", " M generated.txt"])

    result = PushService(
        config(direct_main_push_enabled=True, required_test_commands=["python -m pytest"]),
        git,
        FakeTestTool(),
    ).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert result.local_commit_created is True
    assert "workspace became dirty after required tests" in result.errors
    assert ("push_default_branch", None) not in git.calls


def test_push_service_cherry_pick_failure_fails_without_commit_or_push() -> None:
    git = FakeGitTool(cherry_pick_ok=False)

    result = PushService(config(direct_main_push_enabled=True), git, FakeTestTool()).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert result.local_commit_created is False
    assert any("conflict" in error for error in result.errors)
    assert all(call[0] != "commit_direct_main" for call in git.calls)
    assert ("push_default_branch", None) not in git.calls


def test_push_service_final_diff_protected_path_fails_without_commit() -> None:
    git = FakeGitTool(
        final_diff=DiffStats(changed_files=["infra/prod/main.tf"], touched_protected_paths=["infra/prod/main.tf"])
    )

    result = PushService(
        config(direct_main_push_enabled=True, protected_paths=["infra/prod"]),
        git,
        FakeTestTool(),
    ).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert result.local_commit_created is False
    assert any("protected paths touched" in error for error in result.errors)
    assert all(call[0] != "commit_direct_main" for call in git.calls)
    assert ("push_default_branch", None) not in git.calls


def test_push_service_final_diff_secrets_touched_fails_without_commit() -> None:
    git = FakeGitTool(final_diff=DiffStats(changed_files=["config/.env"], secrets_touched=True))

    result = PushService(
        config(direct_main_push_enabled=True),
        git,
        FakeTestTool(),
    ).push_direct_main(**inputs())  # type: ignore[arg-type]

    assert result.status == ReportStatus.FAILED
    assert result.local_commit_created is False
    assert "secrets touched" in result.errors
    assert all(call[0] != "commit_direct_main" for call in git.calls)
    assert ("push_default_branch", None) not in git.calls
