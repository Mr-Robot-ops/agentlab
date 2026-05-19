from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import CommandResult
from agentlab.policies.command_policy import CommandPolicy, CommandPolicyError
from agentlab.tools.common import run_subprocess


class TestTool:
    def __init__(self, repo_path: str | Path, config: AppConfig) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.config = config
        self.policy = CommandPolicy(
            allowed_commands=config.allowed_commands,
            forbidden_commands=config.forbidden_commands,
        )

    def is_allowed(self, command: str) -> bool:
        return self.policy.is_allowed(command)

    def run_command(self, command: str, *, timeout_seconds: int | None = None) -> CommandResult:
        try:
            parsed = self.policy.parse(command)
        except CommandPolicyError as exc:
            return CommandResult(command=command, cwd=str(self.repo_path), exit_code=126, stderr=str(exc))
        return run_subprocess(
            parsed.argv,
            cwd=self.repo_path,
            timeout_seconds=timeout_seconds or self.config.command_timeout_seconds,
        )
