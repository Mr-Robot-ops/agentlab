from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import CommandResult
from agentlab.tools.common import ToolError, run_subprocess


class DockerTool:
    def __init__(self, repo_path: str | Path, config: AppConfig) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.config = config

    def docker_build(self, tag: str = "agentlab-local:latest", dockerfile: str = "Dockerfile") -> CommandResult:
        if not (self.repo_path / dockerfile).exists():
            return CommandResult(command="docker build", cwd=str(self.repo_path), exit_code=0, stderr="Dockerfile not present")
        if ".." in Path(dockerfile).parts:
            raise ToolError("unsafe Dockerfile path")
        return run_subprocess(
            ["docker", "build", "-f", dockerfile, "-t", tag, "."],
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
        )

    def docker_compose_config(self, compose_file: str = "docker-compose.yml") -> CommandResult:
        if not (self.repo_path / compose_file).exists():
            return CommandResult(command="docker compose config", cwd=str(self.repo_path), exit_code=0, stderr="compose file not present")
        return run_subprocess(
            ["docker", "compose", "-f", compose_file, "config"],
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
        )

    def docker_compose_up(self, compose_file: str = "docker-compose.yml") -> CommandResult:
        return run_subprocess(
            ["docker", "compose", "-f", compose_file, "up", "-d"],
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
        )

    def docker_logs(self, service: str | None = None, compose_file: str = "docker-compose.yml") -> CommandResult:
        command = ["docker", "compose", "-f", compose_file, "logs", "--no-color"]
        if service:
            command.append(service)
        return run_subprocess(command, cwd=self.repo_path, timeout_seconds=self.config.command_timeout_seconds)

    def docker_down(self, compose_file: str = "docker-compose.yml") -> CommandResult:
        return run_subprocess(
            ["docker", "compose", "-f", compose_file, "down"],
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
        )
