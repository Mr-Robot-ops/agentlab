from __future__ import annotations

from pathlib import Path

import pytest

from agentlab.config import AppConfig
from agentlab.models import CommandResult
from agentlab.tools.common import ToolError
from agentlab.tools.docker_tool import DockerTool


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=tmp_path,
        workspace_root=tmp_path / "runs",
    )


@pytest.mark.parametrize("compose_file", ["../docker-compose.yml", "/tmp/docker-compose.yml", "nested/compose.yaml"])
def test_docker_tool_blocks_unsafe_compose_paths(tmp_path: Path, compose_file: str) -> None:
    tool = DockerTool(tmp_path, config(tmp_path))

    with pytest.raises(ToolError):
        tool._safe_compose_file(compose_file)


@pytest.mark.parametrize("compose_file", ["compose.yaml", "docker-compose.yml"])
def test_docker_tool_allows_known_compose_filenames(tmp_path: Path, compose_file: str) -> None:
    tool = DockerTool(tmp_path, config(tmp_path))

    assert tool._safe_compose_file(compose_file) == compose_file


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("docker_compose_config", {}),
        ("docker_compose_up", {}),
        ("docker_logs", {"service": "app"}),
        ("docker_down", {}),
    ],
)
def test_docker_tool_public_compose_methods_block_unsafe_paths(
    tmp_path: Path,
    method: str,
    kwargs: dict[str, str],
) -> None:
    tool = DockerTool(tmp_path, config(tmp_path))

    with pytest.raises(ToolError):
        getattr(tool, method)(compose_file="nested/compose.yaml", **kwargs)


@pytest.mark.parametrize(
    "method,kwargs,expected_tail",
    [
        ("docker_compose_config", {}, ["config"]),
        ("docker_compose_up", {}, ["up", "-d"]),
        ("docker_logs", {"service": "app"}, ["logs", "--no-color", "app"]),
        ("docker_down", {}, ["down"]),
    ],
)
def test_docker_tool_public_compose_methods_accept_allowlisted_root_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    kwargs: dict[str, str],
    expected_tail: list[str],
) -> None:
    (tmp_path / "compose.yaml").write_text("services:\n  app:\n    image: alpine\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run_subprocess(command: list[str], **_: object) -> CommandResult:
        calls.append(command)
        return CommandResult(command=" ".join(command), cwd=str(tmp_path), exit_code=0)

    monkeypatch.setattr("agentlab.tools.docker_tool.run_subprocess", fake_run_subprocess)
    tool = DockerTool(tmp_path, config(tmp_path))

    result = getattr(tool, method)(compose_file="compose.yaml", **kwargs)

    assert result.ok is True
    assert calls == [["docker", "compose", "-f", "compose.yaml", *expected_tail]]
