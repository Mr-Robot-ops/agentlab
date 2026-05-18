from __future__ import annotations

import shutil

from agentlab.models import BuildSecurityReport, Finding, FindingSeverity, ReportStatus
from agentlab.tools.docker_tool import DockerTool
from agentlab.tools.file_tool import FileTool
from agentlab.tools.test_tool import TestTool


class BuildSecurityTestAgent:
    name = "build_security_test"

    def __init__(
        self,
        file_tool: FileTool,
        docker_tool: DockerTool,
        test_tool: TestTool,
        *,
        docker_build_enabled: bool = True,
        docker_compose_enabled: bool = True,
    ) -> None:
        self.file_tool = file_tool
        self.docker_tool = docker_tool
        self.test_tool = test_tool
        self.docker_build_enabled = docker_build_enabled
        self.docker_compose_enabled = docker_compose_enabled

    def run(self) -> BuildSecurityReport:
        files = set(self.file_tool.list_files())
        docker_build = self.docker_tool.docker_build() if self.docker_build_enabled and "Dockerfile" in files else None
        compose_name = "docker-compose.yml" if "docker-compose.yml" in files else "compose.yaml"
        compose_config = (
            self.docker_tool.docker_compose_config(compose_name)
            if self.docker_compose_enabled and compose_name in files
            else None
        )
        scanner_commands = self._scanner_commands(files)
        scanner_results = [self.test_tool.run_command(command) for command in scanner_commands]
        findings: list[Finding] = []
        for result in scanner_results:
            output = (result.stdout + "\n" + result.stderr).lower()
            if result.exit_code != 0:
                severity = FindingSeverity.CRITICAL if "critical" in output or "secret" in output else FindingSeverity.HIGH
                findings.append(
                    Finding(
                        tool=result.command.split()[0],
                        severity=severity,
                        title=f"{result.command} reported findings",
                        description=(result.stdout + "\n" + result.stderr)[-2000:],
                        blocked=severity == FindingSeverity.CRITICAL,
                    )
                )
        command_results = [item for item in [docker_build, compose_config] if item is not None] + scanner_results
        passed = all(result.ok for result in command_results) and not any(finding.blocked for finding in findings)
        return BuildSecurityReport(
            status=ReportStatus.PASSED if passed else ReportStatus.FAILED,
            passed=passed,
            docker_build=docker_build,
            compose_config=compose_config,
            scanners=scanner_results,
            findings=findings,
            recommendation="Proceed" if passed else "Resolve build or blocking security findings.",
        )

    def _scanner_commands(self, files: set[str]) -> list[str]:
        commands: list[str] = []
        if shutil.which("trivy") and self.test_tool.is_allowed("trivy fs --exit-code 1 --severity CRITICAL,HIGH ."):
            commands.append("trivy fs --exit-code 1 --severity CRITICAL,HIGH .")
        if shutil.which("gitleaks") and self.test_tool.is_allowed("gitleaks detect --no-banner --redact"):
            commands.append("gitleaks detect --no-banner --redact")
        if shutil.which("semgrep") and self.test_tool.is_allowed("semgrep --config auto --json"):
            commands.append("semgrep --config auto --json")
        if shutil.which("bandit") and any(path.endswith(".py") for path in files) and self.test_tool.is_allowed("bandit -r ."):
            commands.append("bandit -r .")
        if shutil.which("npm") and "package.json" in files and self.test_tool.is_allowed("npm audit"):
            commands.append("npm audit")
        return commands
