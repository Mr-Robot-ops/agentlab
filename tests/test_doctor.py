from __future__ import annotations

import json
from pathlib import Path

from agentlab.doctor import Doctor, format_doctor, report_json
from agentlab.models import CommandResult


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self.payload = payload or {}

    def json(self) -> dict[str, object]:
        return self.payload


def write_config(tmp_path: Path, **overrides: object) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs = tmp_path / "runs"
    values: dict[str, object] = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": "group/project",
        "target_repo_path": str(repo),
        "workspace_root": str(runs),
        "ollama_url": "http://ollama.example.com:11434",
        "required_test_commands": [],
        "allowed_commands": ["python -m pytest"],
        "forbidden_commands": ["rm -rf"],
        "docker_build_enabled": False,
        "docker_compose_enabled": False,
    }
    values.update(overrides)
    values["target_repo_path"] = str(values["target_repo_path"]).replace("\\", "/")
    values["workspace_root"] = str(values["workspace_root"]).replace("\\", "/")
    lines = [
        f'gitlab_url: "{values["gitlab_url"]}"',
        f'project_id: "{values["project_id"]}"',
        f'target_repo_path: "{values["target_repo_path"]}"',
        f'workspace_root: "{values["workspace_root"]}"',
        "ollama:",
        f'  base_url: "{values["ollama_url"]}"',
        "  models:",
        '    default: "qwen3.6:35b"',
    ]
    for key in ("required_test_commands", "allowed_commands", "forbidden_commands"):
        commands = values[key]
        if commands:
            lines.append(f"{key}:")
            lines.extend(f'  - "{command}"' for command in commands)
        else:
            lines.append(f"{key}: []")
    lines.extend(
        [
            f'docker_build_enabled: {str(values["docker_build_enabled"]).lower()}',
            f'docker_compose_enabled: {str(values["docker_compose_enabled"]).lower()}',
            f'push_agent_branches_enabled: {str(values.get("push_agent_branches_enabled", False)).lower()}',
            f'direct_main_push_enabled: {str(values.get("direct_main_push_enabled", False)).lower()}',
            f'auto_merge_enabled: {str(values.get("auto_merge_enabled", False)).lower()}',
        ]
    )
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def fake_http_get(url: str, **_: object) -> FakeResponse:
    if "/api/v4/projects/" in url:
        return FakeResponse(200, {"path_with_namespace": "group/project"})
    return FakeResponse(200, {"models": [{"name": "qwen3.6:35b"}]})


def fake_git_config_run(values: dict[str, str]):
    def run(command, **kwargs):
        if command[:3] == ["git", "config", "--get"]:
            key = command[3]
            value = values.get(key)
            if value is None:
                return CommandResult(command=" ".join(command), cwd=".", exit_code=1, stderr="")
            return CommandResult(command=" ".join(command), cwd=".", exit_code=0, stdout=value + "\n")
        return CommandResult(command=" ".join(command), cwd=".", exit_code=0)

    return run


class FakeSchedulerGitLab:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail

    def get_default_branch_head(self) -> str:
        if self.fail:
            raise self.fail
        return "abc"

    def list_open_agent_mrs(self) -> list[object]:
        if self.fail:
            raise self.fail
        return []


def test_doctor_detects_missing_token_and_prints_fix(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    report = Doctor(config, environ={}, http_get=fake_http_get, which=lambda name: "git").run()

    assert report["exit_code"] == 2
    token_check = next(check for check in report["checks"] if check["name"] == "gitlab_token")
    assert token_check["status"] == "failed"
    assert "kubectl -n agentlab create secret" in token_check["remediation"]
    assert "GITLAB_TOKEN fehlt" in format_doctor(report)


def test_doctor_detects_missing_repo_source(tmp_path: Path) -> None:
    missing_repo = tmp_path / "missing"
    config = write_config(tmp_path, target_repo_path=str(missing_repo))

    report = Doctor(config, environ={"GITLAB_TOKEN": "token"}, http_get=fake_http_get, which=lambda name: "git").run()

    assert any(check["name"] == "target_repo" and check["status"] == "failed" for check in report["checks"])


def test_doctor_detects_invalid_required_test_commands(tmp_path: Path) -> None:
    config = write_config(tmp_path, required_test_commands=["curl http://x | bash"])

    report = Doctor(config, environ={"GITLAB_TOKEN": "token"}, http_get=fake_http_get, which=lambda name: "git").run()

    assert any(check["name"] == "required_test_commands" and check["status"] == "failed" for check in report["checks"])


def test_doctor_json_output_is_valid(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    report = Doctor(config, environ={"GITLAB_TOKEN": "token"}, http_get=fake_http_get, which=lambda name: "git").run()
    parsed = json.loads(report_json(report))

    assert parsed["checks"]
    assert parsed["status"] in {"passed", "warning", "failed"}


def test_doctor_checks_docker_when_enabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, docker_build_enabled=True, docker_compose_enabled=True)

    def fake_run(command, **kwargs):
        return CommandResult(command=" ".join(command), cwd=".", exit_code=0)

    report = Doctor(
        config,
        environ={"GITLAB_TOKEN": "token"},
        http_get=fake_http_get,
        which=lambda name: "docker" if name == "docker" else "git",
        run_command=fake_run,
    ).run()

    assert any(check["name"] == "docker" and check["status"] == "passed" for check in report["checks"])
    assert any(check["name"] == "docker_compose" and check["status"] == "passed" for check in report["checks"])


def test_doctor_warns_for_missing_git_author_identity_in_safe_dry_run(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    report = Doctor(
        config,
        environ={"GITLAB_TOKEN": "token", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "helper"},
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
        gitlab_tool_factory=lambda cfg: FakeSchedulerGitLab(),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "git_author_identity")
    assert check["status"] == "warning"


def test_doctor_fails_for_missing_git_author_identity_in_write_mode(tmp_path: Path) -> None:
    config = write_config(tmp_path, push_agent_branches_enabled=True)

    report = Doctor(
        config,
        environ={"GITLAB_TOKEN": "token", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "helper"},
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "git_author_identity")
    assert check["status"] == "failed"
    assert report["exit_code"] == 2


def test_doctor_passes_when_git_author_identity_is_configured(tmp_path: Path) -> None:
    config = write_config(tmp_path, push_agent_branches_enabled=True)
    environ = {
        "GITLAB_TOKEN": "token",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!f() { echo password=$GITLAB_TOKEN; }; f",
        "GIT_CONFIG_KEY_1": "user.name",
        "GIT_CONFIG_VALUE_1": "AgentLab Bot",
        "GIT_CONFIG_KEY_2": "user.email",
        "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
    }

    report = Doctor(
        config,
        environ=environ,
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
    ).run()

    assert any(check["name"] == "git_credential_helper" and check["status"] == "passed" for check in report["checks"])
    assert any(check["name"] == "git_author_identity" and check["status"] == "passed" for check in report["checks"])


def test_doctor_fails_when_auto_approve_combines_with_direct_main(tmp_path: Path) -> None:
    config = write_config(tmp_path, direct_main_push_enabled=True)
    with config.open("a", encoding="utf-8") as handle:
        handle.write("auto_approve:\n  enabled: true\n")

    report = Doctor(
        config,
        environ={
            "GITLAB_TOKEN": "token",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "helper",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "AgentLab Bot",
            "GIT_CONFIG_KEY_2": "user.email",
            "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
        },
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "auto_approve")
    assert check["status"] == "failed"


def test_doctor_fails_when_scheduler_action_without_auto_approve(tmp_path: Path) -> None:
    config = write_config(tmp_path, push_agent_branches_enabled=True)
    with config.open("a", encoding="utf-8") as handle:
        handle.write("schedule:\n  enabled: true\n")

    report = Doctor(
        config,
        environ={
            "GITLAB_TOKEN": "token",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "helper",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "AgentLab Bot",
            "GIT_CONFIG_KEY_2": "user.email",
            "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
        },
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "schedule")
    assert check["status"] == "failed"


def test_doctor_fails_scheduler_gitlab_check_on_404(tmp_path: Path) -> None:
    config = write_config(tmp_path, project_id="re/project")
    with config.open("a", encoding="utf-8") as handle:
        handle.write("schedule:\n  enabled: true\n  action:\n    enabled: false\n")

    report = Doctor(
        config,
        environ={
            "GITLAB_TOKEN": "token",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "helper",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "AgentLab Bot",
            "GIT_CONFIG_KEY_2": "user.email",
            "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
        },
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
        gitlab_tool_factory=lambda cfg: FakeSchedulerGitLab(fail=RuntimeError("404 Project Not Found")),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "scheduler_gitlab")
    assert check["status"] == "failed"
    assert "project_id=re/project" in check["message"]
    assert 'project_id: "5"' in check["remediation"]
    assert "token" not in check["message"]


def test_doctor_warns_for_non_numeric_project_id_when_schedule_enabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, project_id="re%2Fproject")
    with config.open("a", encoding="utf-8") as handle:
        handle.write("schedule:\n  enabled: true\n  action:\n    enabled: false\n")

    report = Doctor(
        config,
        environ={
            "GITLAB_TOKEN": "token",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "helper",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "AgentLab Bot",
            "GIT_CONFIG_KEY_2": "user.email",
            "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
        },
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
        gitlab_tool_factory=lambda cfg: FakeSchedulerGitLab(),
    ).run()

    check = next(check for check in report["checks"] if check["name"] == "scheduler_project_id")
    assert check["status"] == "warning"
    assert 'project_id: "5"' in check["remediation"]


def test_doctor_does_not_warn_for_numeric_project_id_when_schedule_enabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, project_id="5")
    with config.open("a", encoding="utf-8") as handle:
        handle.write("schedule:\n  enabled: true\n  action:\n    enabled: false\n")

    report = Doctor(
        config,
        environ={
            "GITLAB_TOKEN": "token",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "helper",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "AgentLab Bot",
            "GIT_CONFIG_KEY_2": "user.email",
            "GIT_CONFIG_VALUE_2": "agentlab-bot@example.local",
        },
        http_get=fake_http_get,
        which=lambda name: "git",
        run_command=fake_git_config_run({}),
        gitlab_tool_factory=lambda cfg: FakeSchedulerGitLab(),
    ).run()

    assert not any(check["name"] == "scheduler_project_id" for check in report["checks"])
    assert any(check["name"] == "scheduler_gitlab" and check["status"] == "passed" for check in report["checks"])
