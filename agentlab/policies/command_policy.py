from __future__ import annotations

import shlex
from dataclasses import dataclass


class CommandPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCommand:
    raw: str
    argv: list[str]

    @property
    def executable(self) -> str:
        return self.argv[0] if self.argv else ""


class CommandPolicy:
    SHELL_META = (";", "&&", "||", "|", ">", "<", "$(", "`", "\n", "\r")

    def __init__(self, *, allowed_commands: list[str], forbidden_commands: list[str]) -> None:
        self.allowed_commands = [command.strip() for command in allowed_commands if command.strip()]
        self.forbidden_commands = [command.strip() for command in forbidden_commands if command.strip()]

    def parse(self, command: str) -> ParsedCommand:
        raw = command.strip()
        if not raw:
            raise CommandPolicyError("empty command")
        lowered = raw.lower()
        if any(meta in raw for meta in self.SHELL_META):
            raise CommandPolicyError("shell metacharacters are not allowed")
        for forbidden in self.forbidden_commands:
            if forbidden.lower() in lowered:
                raise CommandPolicyError(f"forbidden command: {forbidden}")
        if not self._allowed(raw):
            raise CommandPolicyError("command is not allowlisted")
        try:
            argv = shlex.split(raw, posix=False)
        except ValueError as exc:
            raise CommandPolicyError(f"could not parse command: {exc}") from exc
        if not argv:
            raise CommandPolicyError("empty command")
        return ParsedCommand(raw=raw, argv=argv)

    def is_allowed(self, command: str) -> bool:
        try:
            self.parse(command)
        except CommandPolicyError:
            return False
        return True

    def _allowed(self, command: str) -> bool:
        lowered = command.lower()
        for allowed in self.allowed_commands:
            allowed_lower = allowed.lower()
            if lowered == allowed_lower or lowered.startswith(allowed_lower + " "):
                return True
        return False
