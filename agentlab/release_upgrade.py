from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentlab.k8s_operator import (
    DEFAULT_MANIFEST_DIR,
    DEFAULT_NAMESPACE,
    ClusterStatus,
    K8sOperator,
    K8sOperatorError,
    format_status,
    format_upgrade_report,
)


@dataclass
class ReleaseCommandResult:
    args: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class ReleaseCommandRunner(Protocol):
    def run(self, args: list[str], *, cwd: Path) -> ReleaseCommandResult:
        ...


class SubprocessReleaseCommandRunner:
    def run(self, args: list[str], *, cwd: Path) -> ReleaseCommandResult:
        try:
            completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            return ReleaseCommandResult(args=args, stderr=str(exc), returncode=127)
        return ReleaseCommandResult(
            args=args,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
        )


@dataclass
class ReleaseUpgradeOptions:
    image: str
    repo: Path = field(default_factory=Path.cwd)
    manifest_dir: Path = DEFAULT_MANIFEST_DIR
    namespace: str = DEFAULT_NAMESPACE
    skip_git_pull: bool = False
    allow_dirty: bool = False
    skip_tests: bool = False
    test_command: list[str] | None = None
    skip_build: bool = False
    skip_push: bool = False
    docker_bin: str = "docker"
    build_args: list[str] = field(default_factory=list)
    platform: str | None = None
    apply: bool = False
    preserve_cluster_config: bool | None = None
    preserve_local_config: bool = False
    run_doctor: bool | None = None
    cleanup_failed: bool | None = None
    status: bool | None = None
    dry_run: bool = False

    def resolved_repo(self) -> Path:
        return self.repo.resolve()

    def resolved_manifest_dir(self) -> Path:
        if self.manifest_dir.is_absolute():
            return self.manifest_dir
        return self.resolved_repo() / self.manifest_dir

    def effective_preserve_cluster_config(self) -> bool:
        if self.preserve_cluster_config is not None:
            return self.preserve_cluster_config
        return self.apply and not self.preserve_local_config

    def effective_run_doctor(self) -> bool:
        return self.apply if self.run_doctor is None else self.run_doctor

    def effective_cleanup_failed(self) -> bool:
        return self.apply if self.cleanup_failed is None else self.cleanup_failed

    def effective_status(self) -> bool:
        return self.apply if self.status is None else self.status

    def pytest_command(self) -> list[str]:
        return self.test_command or [sys.executable, "-m", "pytest"]

    def docker_build_command(self) -> list[str]:
        command = [self.docker_bin, "build", "-t", self.image]
        if self.platform:
            command.extend(["--platform", self.platform])
        for build_arg in self.build_args:
            command.extend(["--build-arg", build_arg])
        command.append(".")
        return command

    def docker_push_command(self) -> list[str]:
        return [self.docker_bin, "push", self.image]

    def k8s_upgrade_command(self) -> list[str]:
        command = [
            "agentlab",
            "k8s",
            "upgrade",
            "--image",
            self.image,
            "--namespace",
            self.namespace,
            "--manifest-dir",
            str(self.manifest_dir),
        ]
        if self.apply:
            command.append("--apply")
        if self.effective_preserve_cluster_config():
            command.append("--preserve-cluster-config")
        if self.preserve_local_config:
            command.append("--preserve-local-config")
        if self.effective_run_doctor():
            command.append("--run-doctor")
        if self.effective_cleanup_failed():
            command.append("--cleanup-failed")
        if self.effective_status():
            command.append("--status")
        if self.apply:
            command.append("--yes")
        return command

    def k8s_status_command(self) -> list[str]:
        return [
            "agentlab",
            "k8s",
            "status",
            "--namespace",
            self.namespace,
            "--manifest-dir",
            str(self.manifest_dir),
        ]


@dataclass
class ReleaseStep:
    name: str
    status: str
    command: list[str] | None = None
    detail: str = ""
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass
class ReleaseUpgradeReport:
    image: str
    repo: str
    namespace: str
    manifest_dir: str
    dry_run: bool = False
    steps: list[ReleaseStep] = field(default_factory=list)

    @property
    def failed_step(self) -> ReleaseStep | None:
        return next((step for step in self.steps if step.status == "failed"), None)


class ReleaseUpgradeError(RuntimeError):
    def __init__(self, message: str, report: ReleaseUpgradeReport) -> None:
        self.report = report
        super().__init__(message)


class ReleaseUpgrader:
    def __init__(
        self,
        *,
        command_runner: ReleaseCommandRunner | None = None,
        operator_factory: Any | None = None,
    ) -> None:
        self.command_runner = command_runner or SubprocessReleaseCommandRunner()
        self.operator_factory = operator_factory or (lambda namespace, manifest_dir: K8sOperator(namespace=namespace, manifest_dir=manifest_dir))

    def run(self, options: ReleaseUpgradeOptions) -> ReleaseUpgradeReport:
        repo = options.resolved_repo()
        manifest_dir = options.resolved_manifest_dir()
        report = ReleaseUpgradeReport(
            image=options.image,
            repo=str(repo),
            namespace=options.namespace,
            manifest_dir=str(manifest_dir),
            dry_run=options.dry_run,
        )
        self._validate_options(options, report)
        if options.dry_run:
            self._add_dry_run_steps(options, report)
            return report

        status = self._run_command(report, "Git status", ["git", "status", "--porcelain"], cwd=repo)
        dirty = bool(status.stdout.strip())
        if dirty and not options.allow_dirty:
            status.status = "failed"
            status.detail = "dirty working tree"
            raise ReleaseUpgradeError("Working tree is dirty. Use --allow-dirty to continue.", report)
        status.detail = "dirty allowed" if dirty else "clean"

        if options.skip_git_pull:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="--skip-git-pull"))
        else:
            self._run_command(report, "Git pull", ["git", "pull"], cwd=repo)

        if options.skip_tests:
            report.steps.append(ReleaseStep(name="Tests", status="skipped", detail="--skip-tests"))
        else:
            self._run_command(report, "Tests", options.pytest_command(), cwd=repo)

        if options.skip_build:
            report.steps.append(ReleaseStep(name="Docker build", status="skipped", detail="--skip-build"))
        else:
            self._run_command(report, "Docker build", options.docker_build_command(), cwd=repo)

        if options.skip_push:
            report.steps.append(ReleaseStep(name="Docker push", status="skipped", detail="--skip-push"))
        else:
            self._run_command(report, "Docker push", options.docker_push_command(), cwd=repo)

        operator = self.operator_factory(options.namespace, manifest_dir)
        try:
            upgrade_report = operator.upgrade(
                image=options.image,
                apply=options.apply,
                preserve_cluster_config=options.effective_preserve_cluster_config(),
                preserve_local_config=options.preserve_local_config,
                run_doctor=options.effective_run_doctor(),
                show_status=options.effective_status(),
                cleanup_failed=options.effective_cleanup_failed(),
            )
        except K8sOperatorError as exc:
            report.steps.append(
                ReleaseStep(
                    name="Kubernetes upgrade",
                    status="failed",
                    command=options.k8s_upgrade_command(),
                    detail=str(exc),
                )
            )
            raise ReleaseUpgradeError("Kubernetes upgrade failed.", report) from exc
        upgrade_output = format_upgrade_report(upgrade_report)
        upgrade_step = ReleaseStep(
            name="Kubernetes upgrade",
            status="passed",
            command=options.k8s_upgrade_command(),
            stdout=upgrade_output,
        )
        report.steps.append(upgrade_step)
        if getattr(upgrade_report, "image_drift", []):
            upgrade_step.status = "failed"
            upgrade_step.detail = "image drift remains"
            raise ReleaseUpgradeError("Kubernetes upgrade reported image drift.", report)

        if options.effective_status():
            try:
                cluster_status: ClusterStatus = operator.status(manifest_dir=manifest_dir)
            except K8sOperatorError as exc:
                report.steps.append(
                    ReleaseStep(
                        name="Status",
                        status="failed",
                        command=options.k8s_status_command(),
                        detail=str(exc),
                    )
                )
                raise ReleaseUpgradeError("Kubernetes status failed.", report) from exc
            status_output = format_status(cluster_status)
            status_step = ReleaseStep(
                name="Status",
                status="passed",
                command=options.k8s_status_command(),
                stdout=status_output,
            )
            report.steps.append(status_step)
            if _status_has_image_drift(cluster_status):
                status_step.status = "failed"
                status_step.detail = "image drift remains"
                raise ReleaseUpgradeError("Kubernetes status reported image drift.", report)
        else:
            report.steps.append(ReleaseStep(name="Status", status="skipped", detail="status not requested"))
        return report

    def _validate_options(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport) -> None:
        if options.effective_preserve_cluster_config() and options.preserve_local_config:
            report.steps.append(
                ReleaseStep(
                    name="Validate options",
                    status="failed",
                    detail="Choose either --preserve-cluster-config or --preserve-local-config, not both.",
                )
            )
            raise ReleaseUpgradeError("Choose either --preserve-cluster-config or --preserve-local-config, not both.", report)

    def _add_dry_run_steps(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport) -> None:
        report.steps.append(ReleaseStep(name="Git status", status="planned", command=["git", "status", "--porcelain"]))
        if options.skip_git_pull:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="--skip-git-pull"))
        else:
            report.steps.append(ReleaseStep(name="Git pull", status="planned", command=["git", "pull"]))
        if options.skip_tests:
            report.steps.append(ReleaseStep(name="Tests", status="skipped", detail="--skip-tests"))
        else:
            report.steps.append(ReleaseStep(name="Tests", status="planned", command=options.pytest_command()))
        if options.skip_build:
            report.steps.append(ReleaseStep(name="Docker build", status="skipped", detail="--skip-build"))
        else:
            report.steps.append(ReleaseStep(name="Docker build", status="planned", command=options.docker_build_command()))
        if options.skip_push:
            report.steps.append(ReleaseStep(name="Docker push", status="skipped", detail="--skip-push"))
        else:
            report.steps.append(ReleaseStep(name="Docker push", status="planned", command=options.docker_push_command()))
        report.steps.append(ReleaseStep(name="Kubernetes upgrade", status="planned", command=options.k8s_upgrade_command()))
        if options.effective_status():
            report.steps.append(ReleaseStep(name="Status", status="planned", command=options.k8s_status_command()))
        else:
            report.steps.append(ReleaseStep(name="Status", status="skipped", detail="status not requested"))

    def _run_command(self, report: ReleaseUpgradeReport, name: str, command: list[str], *, cwd: Path) -> ReleaseStep:
        result = self.command_runner.run(command, cwd=cwd)
        step = ReleaseStep(
            name=name,
            status="passed" if result.returncode == 0 else "failed",
            command=result.args,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        report.steps.append(step)
        if result.returncode != 0:
            raise ReleaseUpgradeError(f"{name} failed.", report)
        return step


def parse_command_text(value: str | None) -> list[str] | None:
    if not value:
        return None
    return shlex.split(value, posix=os.name != "nt")


def format_release_report(report: ReleaseUpgradeReport) -> str:
    lines = [
        "AgentLab release upgrade",
        "",
        f"Image: {report.image}",
        f"Repo: {report.repo}",
        f"Namespace: {report.namespace}",
        f"Manifest dir: {report.manifest_dir}",
        "",
        "Steps:",
    ]
    for index, step in enumerate(report.steps, start=1):
        status = step.detail or step.status
        lines.append(f"{index}. {step.name}: {status}")
        if step.command:
            lines.append(f"   Command: {_format_command(step.command)}")

    failed = report.failed_step
    if failed is not None:
        lines.extend(["", "Failure:"])
        lines.append(f"- failed step: {failed.name}")
        if failed.command:
            lines.append(f"- command: {_format_command(failed.command)}")
        if failed.returncode is not None:
            lines.append(f"- exit code: {failed.returncode}")
        snippet = _stderr_snippet(failed.stderr or failed.detail)
        if snippet:
            lines.append(f"- stderr: {snippet}")
    return "\n".join(lines)


def _format_command(command: list[str]) -> str:
    return shlex.join(str(part) for part in command)


def _stderr_snippet(value: str, *, limit: int = 500) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _status_has_image_drift(status: ClusterStatus) -> bool:
    return any(item.image_drift for item in status.cronjobs) or bool(status.manifest_image_drifts)
