from __future__ import annotations

import re
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
        cd_command = self._safe_cd_command(command)
        if cd_command is not None:
            _, inner = cd_command
            return self.policy.is_allowed(inner)
        return self.policy.is_allowed(command)

    def run_command(self, command: str, *, timeout_seconds: int | None = None) -> CommandResult:
        cwd = self.repo_path
        parsed_command = command
        cd_command = self._safe_cd_command(command)
        if cd_command is not None:
            cwd, parsed_command = cd_command
        try:
            parsed = self.policy.parse(parsed_command)
        except CommandPolicyError as exc:
            return CommandResult(command=command, cwd=str(cwd), exit_code=126, stderr=str(exc))
        return run_subprocess(
            parsed.argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds or self.config.command_timeout_seconds,
            env=self.config.functional_test_env or None,
        ).model_copy(update={"command": command, "cwd": str(cwd)})

    def _safe_cd_command(self, command: str) -> tuple[Path, str] | None:
        match = re.fullmatch(r"\s*cd\s+([A-Za-z0-9._/-]+)\s+&&\s+(.+?)\s*", command)
        if not match:
            return None
        relative = Path(match.group(1))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        cwd = (self.repo_path / relative).resolve()
        try:
            cwd.relative_to(self.repo_path)
        except ValueError:
            return None
        return cwd, match.group(2)
