from __future__ import annotations

import shlex
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import CommandResult
from agentlab.tools.common import run_subprocess


class TestTool:
    SHELL_META = (";", "&&", "||", "|", ">", "<", "$(", "`")

    def __init__(self, repo_path: str | Path, config: AppConfig) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.config = config

    def is_allowed(self, command: str) -> bool:
        lowered = command.lower().strip()
        if any(item.lower() in lowered for item in self.config.forbidden_commands):
            return False
        if any(meta in command for meta in self.SHELL_META):
            return False
        return any(lowered == allowed.lower() or lowered.startswith(allowed.lower() + " ") for allowed in self.config.allowed_commands)

    def run_command(self, command: str, *, timeout_seconds: int | None = None) -> CommandResult:
        if not self.is_allowed(command):
            return CommandResult(command=command, cwd=str(self.repo_path), exit_code=126, stderr="command not allowed")
        args = shlex.split(command, posix=False)
        return run_subprocess(
            args,
            cwd=self.repo_path,
            timeout_seconds=timeout_seconds or self.config.command_timeout_seconds,
        )
