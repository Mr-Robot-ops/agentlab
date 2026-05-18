from __future__ import annotations

from pathlib import Path

from agentlab.models import ReportStatus, TestReport
from agentlab.tools.file_tool import FileTool
from agentlab.tools.test_tool import TestTool


class FunctionalTestAgent:
    name = "functional_test"

    def __init__(self, file_tool: FileTool, test_tool: TestTool) -> None:
        self.file_tool = file_tool
        self.test_tool = test_tool

    def run(self) -> TestReport:
        commands = self.detect_commands()
        if not commands:
            return TestReport(status=ReportStatus.SKIPPED, passed=False, logs_excerpt="", recommendation="No known test command detected.")
        results = [self.test_tool.run_command(command) for command in commands]
        passed = all(result.ok for result in results)
        logs = "\n".join((result.stdout + "\n" + result.stderr).strip() for result in results)
        return TestReport(
            status=ReportStatus.PASSED if passed else ReportStatus.FAILED,
            passed=passed,
            commands=results,
            logs_excerpt=logs[-4000:],
            coverage_note="Coverage is reported only when the project test command emits coverage output.",
            recommendation="Proceed" if passed else "Fix failing tests before merge.",
        )

    def detect_commands(self) -> list[str]:
        files = set(self.file_tool.list_files())
        commands: list[str] = []
        if "pyproject.toml" in files or "pytest.ini" in files or any(Path(path).name.startswith("test_") for path in files):
            commands.append("python -m pytest")
        if "pnpm-lock.yaml" in files:
            commands.append("pnpm test")
        elif "package.json" in files:
            commands.append("npm test")
        if "go.mod" in files:
            commands.append("go test ./...")
        if "Cargo.toml" in files:
            commands.append("cargo test")
        return [command for command in commands if self.test_tool.is_allowed(command)]
