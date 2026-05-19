from __future__ import annotations

from pathlib import Path

import pytest

from agentlab.config import AppConfig
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
