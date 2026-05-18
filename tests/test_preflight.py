from pathlib import Path

from agentlab.config import AppConfig
from agentlab.preflight import PreflightChecker


def config(tmp_path: Path, **overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": tmp_path / "repo",
        "workspace_root": tmp_path / "runs",
        "allowed_commands": ["python -m pytest"],
        "forbidden_commands": [],
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def test_preflight_fails_write_mode_without_required_repo_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cfg = config(tmp_path, require_repo_policy_for_write=True)

    report = PreflightChecker(cfg, mode="run-task").run()

    assert report.passed is False
    assert any(check.name == "repo_policy" and check.status == "failed" for check in report.checks)


def test_preflight_warns_when_clone_policy_cannot_be_verified_yet(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        clone_target_repo=True,
        target_repo_url="https://gitlab.example.com/group/project.git",
        require_repo_policy_for_write=True,
    )

    report = PreflightChecker(cfg, mode="run-task").run()

    assert any(check.name == "repo_policy" and check.status == "warning" for check in report.checks)


def test_preflight_fails_required_command_not_allowlisted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cfg = config(tmp_path, required_test_commands=["npm test"])

    report = PreflightChecker(cfg, mode="run-task").run()

    assert report.passed is False
    assert any(check.name == "required_test_commands" for check in report.checks)
