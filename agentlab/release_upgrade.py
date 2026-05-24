from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

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


SEMVER_RE = re.compile(r"^(?P<prefix>v?)(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
K8S_VERSION_SOURCE = "version annotation"
DEFAULT_RELEASE_STATE_FILE = Path(".agentlab/release-state.json")
VERIFY_IMAGE_METHODS = {"manifest", "pull"}
STATE_STEP_KEYS = {
    "Git status": "git_status",
    "Git pull": "git_pull",
    "Git status after pull": "git_status_after_pull",
    "Kubernetes bootstrap": "kubernetes_bootstrap",
    "Kubernetes manifest preflight": "manifest_preflight",
    "Tests": "tests",
    "Git tag": "git_tag",
    "Docker build": "docker_build",
    "Docker push": "docker_push",
    "Image verify": "image_verify",
    "Kubernetes upgrade": "k8s_upgrade",
    "Status": "status",
    "Git tag push": "git_tag_push",
}
DEFAULT_STATE_STEPS = {
    "git_status": "pending",
    "git_pull": "pending",
    "git_status_after_pull": "pending",
    "manifest_preflight": "pending",
    "tests": "pending",
    "git_tag": "pending",
    "docker_build": "pending",
    "docker_push": "pending",
    "image_verify": "pending",
    "k8s_upgrade": "pending",
    "status": "pending",
    "git_tag_push": "pending",
}


@dataclass(frozen=True, order=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int

    @property
    def tag(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    @property
    def image_tag(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ResolvedRelease:
    image: str
    current_version: str | None = None
    current_image: str | None = None
    release_version: str | None = None
    git_tag: str | None = None
    image_repository: str | None = None
    version_source: str = ""


@dataclass
class ReleaseState:
    version: str
    image: str
    repo: str
    commit: str
    namespace: str
    manifest_dir: str
    verify_image_method: str
    docker_bin: str = "docker"
    steps: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATE_STEPS))
    schema_version: int = 1
    completed: bool = False

    @classmethod
    def from_file(cls, path: Path) -> "ReleaseState":
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = dict(DEFAULT_STATE_STEPS)
        steps.update(data.get("steps") or {})
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            version=str(data["version"]),
            image=str(data["image"]),
            repo=str(data.get("repo", path.parent.parent)),
            commit=str(data.get("commit", "unknown")),
            namespace=str(data.get("namespace", DEFAULT_NAMESPACE)),
            manifest_dir=str(data.get("manifest_dir", DEFAULT_MANIFEST_DIR)),
            verify_image_method=str(data.get("verify_image_method", "manifest")),
            docker_bin=str(data.get("docker_bin", "docker")),
            steps=steps,
            completed=bool(data.get("completed", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "image": self.image,
            "repo": self.repo,
            "commit": self.commit,
            "namespace": self.namespace,
            "manifest_dir": self.manifest_dir,
            "verify_image_method": self.verify_image_method,
            "docker_bin": self.docker_bin,
            "steps": self.steps,
            "completed": self.completed,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mark(self, step_key: str, status: str, path: Path) -> None:
        self.steps[step_key] = status
        self.completed = all(value in {"passed", "skipped"} for value in self.steps.values())
        self.write(path)


@dataclass
class ReleaseResumeOptions:
    repo: Path = field(default_factory=Path.cwd)
    state_file: Path = DEFAULT_RELEASE_STATE_FILE
    verify_image_method: str | None = None
    dry_run: bool = False
    allow_head_mismatch: bool = False

    def resolved_repo(self) -> Path:
        return self.repo.resolve()

    def resolved_state_file(self) -> Path:
        if self.state_file.is_absolute():
            return self.state_file
        return self.resolved_repo() / self.state_file


def parse_release_version(value: str) -> ReleaseVersion:
    match = SEMVER_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid semantic version: {value}")
    return ReleaseVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
    )


def bump_release_version(version: ReleaseVersion, *, part: str) -> ReleaseVersion:
    if part == "patch":
        return ReleaseVersion(version.major, version.minor, version.patch + 1)
    if part == "minor":
        return ReleaseVersion(version.major, version.minor + 1, 0)
    if part == "major":
        return ReleaseVersion(version.major + 1, 0, 0)
    raise ValueError(f"Unknown version bump: {part}")


def image_repository_from_image(image: str) -> str:
    head, separator, tail = image.rpartition(":")
    if separator and "/" not in tail:
        return head
    if "@" in image:
        return image.split("@", 1)[0]
    return image


def version_from_image(image: str) -> ReleaseVersion | None:
    _head, separator, tail = image.rpartition(":")
    if not separator or "/" in tail:
        return None
    try:
        return parse_release_version(tail)
    except ValueError:
        return None


def image_for_release(repository: str, version: ReleaseVersion, *, prefix_v: bool = False) -> str:
    tag = version.tag if prefix_v else version.image_tag
    return f"{repository}:{tag}"


@dataclass
class ReleaseUpgradeOptions:
    image: str | None = None
    version: str | None = None
    current_version: str | None = None
    bump_patch: bool = False
    bump_minor: bool = False
    bump_major: bool = False
    image_repository: str | None = None
    image_tag_prefix_v: bool = False
    tag: bool = False
    push_tag: bool = False
    github_release: bool = False
    verify_image: bool | None = None
    verify_image_method: str = "manifest"
    push_tag_after_k8s: bool = False
    state_file: Path | None = None
    workflow: str = "upgrade"
    repo: Path = field(default_factory=Path.cwd)
    manifest_dir: Path = DEFAULT_MANIFEST_DIR
    namespace: str = DEFAULT_NAMESPACE
    skip_git_pull: bool = False
    allow_dirty: bool = False
    allow_generated_dirty: bool = True
    pull_ff_only: bool = True
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
    prepare_only: bool = False
    bootstrap_k8s: bool = False
    gitlab_url: str | None = None
    project_id: str | None = None
    target_repo_url: str | None = None
    target_repo_ref: str = "main"
    ollama_url: str | None = None
    model: str = "qwen3.6:35b"
    git_author_name: str = "AgentLab Bot"
    git_author_email: str = "agentlab-bot@example.local"
    mode: str = "safe-dry-run"
    schedule_enabled: bool = False

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

    def is_versioned_release(self) -> bool:
        return bool(self.version or self.bump_patch or self.bump_minor or self.bump_major)

    def effective_verify_image(self) -> bool:
        if self.verify_image is not None:
            return self.verify_image
        return self.is_versioned_release()

    def pytest_command(self) -> list[str]:
        return self.test_command or [sys.executable, "-m", "pytest"]

    def required_image(self) -> str:
        if not self.image:
            raise ValueError("Release image has not been resolved.")
        return self.image

    def git_pull_command(self) -> list[str]:
        return ["git", "pull", "--ff-only"] if self.pull_ff_only else ["git", "pull"]

    def docker_build_command(self) -> list[str]:
        command = [self.docker_bin, "build", "-t", self.required_image()]
        if self.platform:
            command.extend(["--platform", self.platform])
        for build_arg in self.build_args:
            command.extend(["--build-arg", build_arg])
        command.append(".")
        return command

    def docker_push_command(self) -> list[str]:
        return [self.docker_bin, "push", self.required_image()]

    def docker_verify_command(self) -> list[str]:
        return _verify_command(self.docker_bin, self.required_image(), self.verify_image_method)

    def resolved_state_file(self) -> Path | None:
        if self.state_file is None:
            return None
        if self.state_file.is_absolute():
            return self.state_file
        return self.resolved_repo() / self.state_file

    def git_tag_command(self) -> list[str]:
        if not self.version:
            raise ValueError("Release version has not been resolved.")
        return ["git", "tag", self.version]

    def git_push_tag_command(self) -> list[str]:
        if not self.version:
            raise ValueError("Release version has not been resolved.")
        return ["git", "push", "origin", self.version]

    def github_release_command(self, *, commit_sha: str, test_result: str, kubernetes_status: str) -> list[str]:
        if not self.version:
            raise ValueError("Release version has not been resolved.")
        notes = "\n".join(
            [
                f"Image: {self.required_image()}",
                f"Commit: {commit_sha}",
                f"Tests: {test_result}",
                f"Kubernetes upgrade: {kubernetes_status}",
            ]
        )
        return [
            "gh",
            "release",
            "create",
            self.version,
            "--title",
            f"AgentLab {self.version}",
            "--notes",
            notes,
        ]

    def k8s_upgrade_command(self) -> list[str]:
        command = [
            "agentlab",
            "k8s",
            "upgrade",
            "--image",
            self.required_image(),
            "--namespace",
            self.namespace,
            "--manifest-dir",
            str(self.manifest_dir),
        ]
        if self.version:
            command.extend(["--version", self.version])
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

    def bootstrap_command(self) -> list[str]:
        command = [
            sys.executable,
            "scripts/bootstrap_k8s.py",
            "--namespace",
            self.namespace,
            "--image",
            self.required_image(),
            "--gitlab-url",
            self.gitlab_url or "",
            "--project-id",
            self.project_id or "",
            "--target-repo-url",
            self.target_repo_url or "",
            "--target-repo-ref",
            self.target_repo_ref,
            "--ollama-url",
            self.ollama_url or "",
            "--model",
            self.model,
            "--mode",
            self.mode,
            "--git-author-name",
            self.git_author_name,
            "--git-author-email",
            self.git_author_email,
            "--output-dir",
            str(self.manifest_dir),
        ]
        if self.schedule_enabled:
            command.append("--schedule-enabled")
        return command


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
    workflow: str = "upgrade"
    dry_run: bool = False
    current_version: str | None = None
    new_version: str | None = None
    current_image: str | None = None
    image_repository: str | None = None
    version_source: str = ""
    verify_image_method: str = "manifest"
    state_file: str | None = None
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
            image=options.image or "",
            repo=str(repo),
            namespace=options.namespace,
            manifest_dir=str(manifest_dir),
            workflow=options.workflow,
            dry_run=options.dry_run,
            verify_image_method=options.verify_image_method,
            state_file=str(options.resolved_state_file()) if options.resolved_state_file() else None,
        )
        self._validate_options(options, report)
        operator = self.operator_factory(options.namespace, manifest_dir)
        try:
            resolved = self._resolve_release(options, repo=repo, operator=operator)
        except ValueError as exc:
            report.steps.append(ReleaseStep(name="Resolve release", status="failed", detail=str(exc)))
            raise ReleaseUpgradeError("Release version resolution failed.", report) from exc
        options.image = resolved.image
        options.version = resolved.release_version
        report.image = resolved.image
        report.current_version = resolved.current_version
        report.new_version = resolved.release_version
        report.current_image = resolved.current_image
        report.image_repository = resolved.image_repository
        report.version_source = resolved.version_source
        if options.dry_run:
            self._add_dry_run_steps(options, report)
            return report
        state_path = options.resolved_state_file()
        state = self._create_state(options, report, cwd=repo) if state_path else None

        self._check_git_status(report, options, "Git status", cwd=repo, state=state, state_path=state_path)

        if options.skip_git_pull:
            self._refresh_state_commit(state=state, state_path=state_path, cwd=repo)
            self._append_step(report, ReleaseStep(name="Git pull", status="skipped", detail="--skip-git-pull"), state=state, state_path=state_path)
        else:
            self._run_command(report, "Git pull", options.git_pull_command(), cwd=repo, state=state, state_path=state_path)
            self._check_git_status(report, options, "Git status after pull", cwd=repo, state=state, state_path=state_path)
            self._refresh_state_commit(state=state, state_path=state_path, cwd=repo)

        self._ensure_manifest_dir(options, report, repo=repo, manifest_dir=manifest_dir, state=state, state_path=state_path)

        if options.skip_tests:
            self._append_step(report, ReleaseStep(name="Tests", status="skipped", detail="--skip-tests"), state=state, state_path=state_path)
        else:
            self._run_command(report, "Tests", options.pytest_command(), cwd=repo, state=state, state_path=state_path)

        if options.prepare_only:
            if options.tag:
                self._append_step(report, ReleaseStep(name="Git tag", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            self._append_step(report, ReleaseStep(name="Docker build", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            self._append_step(report, ReleaseStep(name="Docker push", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            if options.effective_verify_image():
                self._append_step(report, ReleaseStep(name="Image verify", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            if options.push_tag:
                self._append_step(report, ReleaseStep(name="Git tag push", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            self._append_step(report, ReleaseStep(name="Kubernetes upgrade", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            self._append_step(report, ReleaseStep(name="Status", status="skipped", detail="--prepare-only"), state=state, state_path=state_path)
            return report

        if options.tag:
            self._run_command(report, "Git tag", options.git_tag_command(), cwd=repo, state=state, state_path=state_path)

        if options.skip_build:
            self._append_step(report, ReleaseStep(name="Docker build", status="skipped", detail="--skip-build"), state=state, state_path=state_path)
        else:
            self._run_command(report, "Docker build", options.docker_build_command(), cwd=repo, state=state, state_path=state_path)

        if options.skip_push:
            self._append_step(report, ReleaseStep(name="Docker push", status="skipped", detail="--skip-push"), state=state, state_path=state_path)
        else:
            self._run_command(report, "Docker push", options.docker_push_command(), cwd=repo, state=state, state_path=state_path)

        if options.effective_verify_image():
            self._run_command(report, "Image verify", options.docker_verify_command(), cwd=repo, state=state, state_path=state_path)

        if options.push_tag and not options.push_tag_after_k8s:
            self._run_command(report, "Git tag push", options.git_push_tag_command(), cwd=repo, state=state, state_path=state_path)

        upgrade_kwargs: dict[str, Any] = {
            "image": options.required_image(),
            "apply": options.apply,
            "preserve_cluster_config": options.effective_preserve_cluster_config(),
            "preserve_local_config": options.preserve_local_config,
            "run_doctor": options.effective_run_doctor(),
            "show_status": options.effective_status(),
            "cleanup_failed": options.effective_cleanup_failed(),
        }
        if options.version:
            upgrade_kwargs["version"] = options.version
        try:
            upgrade_report = operator.upgrade(**upgrade_kwargs)
        except K8sOperatorError as exc:
            self._append_step(
                report,
                ReleaseStep(
                    name="Kubernetes upgrade",
                    status="failed",
                    command=options.k8s_upgrade_command(),
                    detail=str(exc),
                ),
                state=state,
                state_path=state_path,
            )
            raise ReleaseUpgradeError("Kubernetes upgrade failed.", report) from exc
        upgrade_output = format_upgrade_report(upgrade_report)
        upgrade_step = ReleaseStep(
            name="Kubernetes upgrade",
            status="passed",
            command=options.k8s_upgrade_command(),
            stdout=upgrade_output,
        )
        self._append_step(report, upgrade_step, state=state, state_path=state_path)
        if getattr(upgrade_report, "image_drift", []):
            upgrade_step.status = "failed"
            upgrade_step.detail = "image drift remains"
            self._mark_state_step(upgrade_step, state=state, state_path=state_path)
            raise ReleaseUpgradeError("Kubernetes upgrade reported image drift.", report)

        if options.effective_status():
            try:
                cluster_status: ClusterStatus = operator.status(manifest_dir=manifest_dir)
            except K8sOperatorError as exc:
                self._append_step(
                    report,
                    ReleaseStep(
                        name="Status",
                        status="failed",
                        command=options.k8s_status_command(),
                        detail=str(exc),
                    ),
                    state=state,
                    state_path=state_path,
                )
                raise ReleaseUpgradeError("Kubernetes status failed.", report) from exc
            status_output = format_status(cluster_status)
            status_step = ReleaseStep(
                name="Status",
                status="passed",
                command=options.k8s_status_command(),
                stdout=status_output,
            )
            self._append_step(report, status_step, state=state, state_path=state_path)
            if _status_has_image_drift(cluster_status):
                status_step.status = "failed"
                status_step.detail = "image drift remains"
                self._mark_state_step(status_step, state=state, state_path=state_path)
                raise ReleaseUpgradeError("Kubernetes status reported image drift.", report)
        else:
            self._append_step(report, ReleaseStep(name="Status", status="skipped", detail="status not requested"), state=state, state_path=state_path)
        if options.push_tag and options.push_tag_after_k8s:
            self._run_command(report, "Git tag push", options.git_push_tag_command(), cwd=repo, state=state, state_path=state_path)
        if options.github_release:
            commit_sha = self._command_stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=repo)
            test_result = _step_status(report, "Tests")
            kubernetes_status = _step_status(report, "Kubernetes upgrade")
            command = options.github_release_command(
                commit_sha=commit_sha,
                test_result=test_result,
                kubernetes_status=kubernetes_status,
            )
            result = self.command_runner.run(command, cwd=repo)
            report.steps.append(
                ReleaseStep(
                    name="GitHub release",
                    status="passed" if result.returncode == 0 else "warning",
                    command=result.args,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    detail="" if result.returncode == 0 else "GitHub Release creation failed; Kubernetes was not rolled back.",
                )
            )
        return report

    def _create_state(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport, *, cwd: Path) -> ReleaseState | None:
        state_path = options.resolved_state_file()
        if state_path is None:
            return None
        commit = self._command_stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=cwd)
        steps = dict(DEFAULT_STATE_STEPS)
        if options.skip_git_pull:
            steps["git_pull"] = "skipped"
            steps["git_status_after_pull"] = "skipped"
        if options.skip_tests:
            steps["tests"] = "skipped"
        if not options.tag:
            steps["git_tag"] = "skipped"
        if options.skip_build:
            steps["docker_build"] = "skipped"
        if options.skip_push:
            steps["docker_push"] = "skipped"
        if not options.effective_verify_image():
            steps["image_verify"] = "skipped"
        if not options.effective_status():
            steps["status"] = "skipped"
        if not options.push_tag:
            steps["git_tag_push"] = "skipped"
        state = ReleaseState(
            version=options.version or "",
            image=options.required_image(),
            repo=str(cwd),
            commit=commit,
            namespace=options.namespace,
            manifest_dir=str(options.manifest_dir),
            verify_image_method=options.verify_image_method,
            docker_bin=options.docker_bin,
            steps=steps,
        )
        state.write(state_path)
        report.state_file = str(state_path)
        return state

    def _refresh_state_commit(
        self,
        *,
        state: ReleaseState | None,
        state_path: Path | None,
        cwd: Path,
    ) -> None:
        if state is None or state_path is None:
            return
        state.commit = self._command_stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=cwd)
        state.write(state_path)

    def _append_step(
        self,
        report: ReleaseUpgradeReport,
        step: ReleaseStep,
        *,
        state: ReleaseState | None,
        state_path: Path | None,
    ) -> ReleaseStep:
        report.steps.append(step)
        self._mark_state_step(step, state=state, state_path=state_path)
        return step

    def _mark_state_step(self, step: ReleaseStep, *, state: ReleaseState | None, state_path: Path | None) -> None:
        if state is None or state_path is None:
            return
        step_key = STATE_STEP_KEYS.get(step.name)
        if step_key is None:
            return
        state.mark(step_key, step.status, state_path)

    def _validate_options(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport) -> None:
        if options.verify_image_method not in VERIFY_IMAGE_METHODS:
            detail = "Invalid --verify-image-method. Choose manifest or pull."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        bump_modes = [
            ("--bump-patch", options.bump_patch),
            ("--bump-minor", options.bump_minor),
            ("--bump-major", options.bump_major),
        ]
        selected_bumps = [name for name, enabled in bump_modes if enabled]
        if len(selected_bumps) > 1:
            detail = "Choose only one version bump mode: --bump-patch, --bump-minor, or --bump-major."
            detail += " Selected: " + ", ".join(selected_bumps)
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if options.image and selected_bumps:
            detail = "--image cannot be combined with --bump-*; use --image alone for manual deploys or --version with --image to write a release annotation."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if options.version and selected_bumps:
            detail = "--version cannot be combined with --bump-*; choose an explicit version or a bump mode."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if not options.image and not options.version and not selected_bumps:
            detail = "Choose a release selection mode: --image, --version, --bump-patch, --bump-minor, or --bump-major."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if options.push_tag and not options.tag:
            detail = "--push-tag requires --tag."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if (options.tag or options.push_tag or options.github_release) and options.image and not options.version:
            detail = "--tag, --push-tag, and --github-release require --version or a --bump-* option; use --image alone for manual deploys."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if options.github_release and not options.tag:
            detail = "--github-release requires --tag."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise ReleaseUpgradeError(detail, report)
        if options.effective_preserve_cluster_config() and options.preserve_local_config:
            report.steps.append(
                ReleaseStep(
                    name="Validate options",
                    status="failed",
                    detail="Choose either --preserve-cluster-config or --preserve-local-config, not both.",
                )
            )
            raise ReleaseUpgradeError("Choose either --preserve-cluster-config or --preserve-local-config, not both.", report)
        if options.bootstrap_k8s:
            missing = [
                name
                for name, value in {
                    "gitlab-url": options.gitlab_url,
                    "project-id": options.project_id,
                    "target-repo-url": options.target_repo_url,
                    "ollama-url": options.ollama_url,
                }.items()
                if not value
            ]
            if missing:
                detail = "Missing required bootstrap options: " + ", ".join(f"--{name}" for name in missing)
                report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
                raise ReleaseUpgradeError(detail, report)

    def _resolve_release(self, options: ReleaseUpgradeOptions, *, repo: Path, operator: K8sOperator) -> ResolvedRelease:
        if options.image:
            release_version = parse_release_version(options.version).tag if options.version else None
            return ResolvedRelease(
                image=options.image,
                release_version=release_version,
                image_repository=image_repository_from_image(options.image),
                version_source="--image + --version" if release_version else "explicit image",
            )

        current_status: ClusterStatus | None = None

        def status() -> ClusterStatus | None:
            nonlocal current_status
            if current_status is not None:
                return current_status
            try:
                current_status = operator.status(manifest_dir=options.resolved_manifest_dir())
            except (K8sOperatorError, OSError):
                current_status = None
            return current_status

        current_version: ReleaseVersion | None = None
        current_version_source = ""
        if options.current_version:
            current_version = parse_release_version(options.current_version)
            current_version_source = "--current-version"

        if current_version is None and (options.bump_patch or options.bump_minor or options.bump_major):
            current_version, current_version_source = self._current_version_for_bump(repo=repo, status=status)

        if options.version:
            new_version = parse_release_version(options.version)
            new_version_source = "--version"
        else:
            if current_version is None:
                raise ValueError("Unable to resolve current release version. Provide --current-version or --version.")
            if options.bump_patch:
                new_version = bump_release_version(current_version, part="patch")
            elif options.bump_minor:
                new_version = bump_release_version(current_version, part="minor")
            elif options.bump_major:
                new_version = bump_release_version(current_version, part="major")
            else:
                raise ValueError("Unable to resolve release version.")
            new_version_source = current_version_source

        cluster_status = current_status if current_status is not None else (None if options.image_repository else status())
        current_image = cluster_status.configmap_image if cluster_status is not None else None
        image_repository = options.image_repository or (image_repository_from_image(current_image) if current_image else None)
        if not image_repository:
            raise ValueError("Unable to infer image repository. Provide --image-repository.")

        return ResolvedRelease(
            image=image_for_release(image_repository, new_version, prefix_v=options.image_tag_prefix_v),
            current_version=current_version.tag if current_version else None,
            current_image=current_image,
            release_version=new_version.tag,
            git_tag=new_version.tag,
            image_repository=image_repository,
            version_source=new_version_source,
        )

    def _current_version_for_bump(
        self,
        *,
        repo: Path,
        status: Callable[[], ClusterStatus | None],
    ) -> tuple[ReleaseVersion, str]:
        latest_git = self._latest_git_tag_version(repo)
        if latest_git is not None:
            return latest_git, "git tag"
        cluster_status = status()
        if cluster_status is not None and cluster_status.configmap_version:
            return parse_release_version(cluster_status.configmap_version), K8S_VERSION_SOURCE
        if cluster_status is not None and cluster_status.configmap_image:
            image_version = version_from_image(cluster_status.configmap_image)
            if image_version is not None:
                source = "deprecated image annotation" if cluster_status.image_annotation_warning else "image annotation"
                return image_version, source
        raise ValueError("Unable to resolve current release version. Provide --current-version.")

    def _latest_git_tag_version(self, repo: Path) -> ReleaseVersion | None:
        result = self.command_runner.run(["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"], cwd=repo)
        if result.returncode != 0:
            return None
        versions: list[ReleaseVersion] = []
        for line in result.stdout.splitlines():
            try:
                versions.append(parse_release_version(line.strip()))
            except ValueError:
                continue
        return max(versions) if versions else None

    def _command_stdout_or_unknown(self, command: list[str], *, cwd: Path) -> str:
        result = self.command_runner.run(command, cwd=cwd)
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"

    def _add_dry_run_steps(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport) -> None:
        report.steps.append(ReleaseStep(name="Git status", status="planned", command=["git", "status", "--porcelain"]))
        if options.skip_git_pull:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="--skip-git-pull"))
        else:
            report.steps.append(ReleaseStep(name="Git pull", status="planned", command=options.git_pull_command()))
            report.steps.append(ReleaseStep(name="Git status after pull", status="planned", command=["git", "status", "--porcelain"]))
        if options.bootstrap_k8s:
            report.steps.append(ReleaseStep(name="Kubernetes bootstrap", status="planned", command=options.bootstrap_command()))
        report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="planned"))
        if options.skip_tests:
            report.steps.append(ReleaseStep(name="Tests", status="skipped", detail="--skip-tests"))
        else:
            report.steps.append(ReleaseStep(name="Tests", status="planned", command=options.pytest_command()))
        if options.prepare_only:
            if options.tag:
                report.steps.append(ReleaseStep(name="Git tag", status="skipped", detail="--prepare-only"))
            report.steps.append(ReleaseStep(name="Docker build", status="skipped", detail="--prepare-only"))
            report.steps.append(ReleaseStep(name="Docker push", status="skipped", detail="--prepare-only"))
            if options.effective_verify_image():
                report.steps.append(ReleaseStep(name="Image verify", status="skipped", detail="--prepare-only"))
            if options.push_tag:
                report.steps.append(ReleaseStep(name="Git tag push", status="skipped", detail="--prepare-only"))
            report.steps.append(ReleaseStep(name="Kubernetes upgrade", status="skipped", detail="--prepare-only"))
            report.steps.append(ReleaseStep(name="Status", status="skipped", detail="--prepare-only"))
            return
        if options.tag:
            report.steps.append(ReleaseStep(name="Git tag", status="planned", command=options.git_tag_command()))
        if options.skip_build:
            report.steps.append(ReleaseStep(name="Docker build", status="skipped", detail="--skip-build"))
        else:
            report.steps.append(ReleaseStep(name="Docker build", status="planned", command=options.docker_build_command()))
        if options.skip_push:
            report.steps.append(ReleaseStep(name="Docker push", status="skipped", detail="--skip-push"))
        else:
            report.steps.append(ReleaseStep(name="Docker push", status="planned", command=options.docker_push_command()))
        if options.effective_verify_image():
            report.steps.append(ReleaseStep(name="Image verify", status="planned", command=options.docker_verify_command()))
        if options.push_tag and not options.push_tag_after_k8s:
            report.steps.append(ReleaseStep(name="Git tag push", status="planned", command=options.git_push_tag_command()))
        report.steps.append(ReleaseStep(name="Kubernetes upgrade", status="planned", command=options.k8s_upgrade_command()))
        if options.apply and options.effective_run_doctor():
            report.steps.append(ReleaseStep(name="Doctor", status="planned"))
        if options.apply and options.effective_cleanup_failed():
            report.steps.append(ReleaseStep(name="Cleanup failed jobs/pods", status="planned"))
        if options.effective_status():
            report.steps.append(ReleaseStep(name="Status", status="planned", command=options.k8s_status_command()))
        else:
            report.steps.append(ReleaseStep(name="Status", status="skipped", detail="status not requested"))
        if options.push_tag and options.push_tag_after_k8s:
            report.steps.append(ReleaseStep(name="Git tag push", status="planned", command=options.git_push_tag_command()))

    def _run_command(
        self,
        report: ReleaseUpgradeReport,
        name: str,
        command: list[str],
        *,
        cwd: Path,
        state: ReleaseState | None = None,
        state_path: Path | None = None,
    ) -> ReleaseStep:
        result = self.command_runner.run(command, cwd=cwd)
        step = ReleaseStep(
            name=name,
            status="passed" if result.returncode == 0 else "failed",
            command=result.args,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self._append_step(report, step, state=state, state_path=state_path)
        if result.returncode != 0:
            raise ReleaseUpgradeError(f"{name} failed.", report)
        return step

    def _check_git_status(
        self,
        report: ReleaseUpgradeReport,
        options: ReleaseUpgradeOptions,
        name: str,
        *,
        cwd: Path,
        state: ReleaseState | None = None,
        state_path: Path | None = None,
    ) -> ReleaseStep:
        step = self._run_command(report, name, ["git", "status", "--porcelain"], cwd=cwd, state=state, state_path=state_path)
        dirty = _dirty_paths(step.stdout)
        if not dirty:
            step.detail = "clean"
            self._mark_state_step(step, state=state, state_path=state_path)
            return step
        if _only_generated_dirty(dirty, repo=cwd, manifest_dir=options.resolved_manifest_dir()) and options.allow_generated_dirty:
            step.detail = "generated manifests dirty allowed"
            self._mark_state_step(step, state=state, state_path=state_path)
            return step
        if options.allow_dirty:
            step.detail = "dirty allowed"
            self._mark_state_step(step, state=state, state_path=state_path)
            return step
        step.status = "failed"
        step.detail = "dirty working tree"
        self._mark_state_step(step, state=state, state_path=state_path)
        raise ReleaseUpgradeError("Working tree is dirty. Use --allow-dirty to continue.", report)

    def _ensure_manifest_dir(
        self,
        options: ReleaseUpgradeOptions,
        report: ReleaseUpgradeReport,
        *,
        repo: Path,
        manifest_dir: Path,
        state: ReleaseState | None = None,
        state_path: Path | None = None,
    ) -> None:
        if manifest_dir.exists() and not manifest_dir.is_dir():
            detail = f"Kubernetes manifest path is not a directory: {manifest_dir}."
            self._append_step(
                report,
                ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail),
                state=state,
                state_path=state_path,
            )
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        if manifest_dir.exists():
            self._append_step(
                report,
                ReleaseStep(name="Kubernetes manifest preflight", status="passed", detail="present"),
                state=state,
                state_path=state_path,
            )
            return
        if not options.bootstrap_k8s:
            detail = f"Kubernetes manifest dir is missing: {manifest_dir}. Run bootstrap first or use --bootstrap-k8s."
            self._append_step(
                report,
                ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail),
                state=state,
                state_path=state_path,
            )
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        self._run_command(report, "Kubernetes bootstrap", options.bootstrap_command(), cwd=repo, state=state, state_path=state_path)
        if not manifest_dir.exists() or not manifest_dir.is_dir():
            detail = f"Kubernetes manifest dir is still missing after bootstrap: {manifest_dir}."
            self._append_step(
                report,
                ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail),
                state=state,
                state_path=state_path,
            )
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        self._append_step(
            report,
            ReleaseStep(name="Kubernetes manifest preflight", status="passed", detail="present after bootstrap"),
            state=state,
            state_path=state_path,
        )


class ReleaseResumer:
    def __init__(
        self,
        *,
        command_runner: ReleaseCommandRunner | None = None,
        operator_factory: Any | None = None,
    ) -> None:
        self.command_runner = command_runner or SubprocessReleaseCommandRunner()
        self.operator_factory = operator_factory or (lambda namespace, manifest_dir: K8sOperator(namespace=namespace, manifest_dir=manifest_dir))

    def run(self, options: ReleaseResumeOptions) -> ReleaseUpgradeReport:
        repo = options.resolved_repo()
        state_path = options.resolved_state_file()
        state = ReleaseState.from_file(state_path)
        verify_method = options.verify_image_method or state.verify_image_method
        report = ReleaseUpgradeReport(
            image=state.image,
            repo=state.repo,
            namespace=state.namespace,
            manifest_dir=str(repo / state.manifest_dir if not Path(state.manifest_dir).is_absolute() else Path(state.manifest_dir)),
            workflow="resume",
            dry_run=options.dry_run,
            current_version=None,
            new_version=state.version,
            verify_image_method=verify_method,
            state_file=str(state_path),
        )
        if verify_method not in VERIFY_IMAGE_METHODS:
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail="Invalid --verify-image-method. Choose manifest or pull."))
            raise ReleaseUpgradeError("Invalid release resume options.", report)
        current_commit = self._command_stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=repo)
        if state.commit != "unknown" and current_commit != state.commit and not options.allow_head_mismatch:
            report.steps.append(
                ReleaseStep(
                    name="Validate state",
                    status="failed",
                    detail=f"HEAD {current_commit} does not match release state commit {state.commit}.",
                )
            )
            raise ReleaseUpgradeError("Release state HEAD mismatch.", report)
        if options.dry_run:
            self._add_dry_run_steps(report, state=state, verify_method=verify_method)
            return report

        docker_bin = state.docker_bin or "docker"
        manifest_dir = Path(state.manifest_dir)
        if not manifest_dir.is_absolute():
            manifest_dir = repo / manifest_dir
        if not _state_step_passed(state, "git_tag"):
            if self._local_tag_exists(state.version, cwd=repo):
                self._mark_state(state, state_path, report, "Git tag", "passed", detail="already exists")
            else:
                self._run_command(report, state, state_path, "Git tag", ["git", "tag", state.version], cwd=repo)
        if not _state_step_passed(state, "docker_build"):
            self._run_command(report, state, state_path, "Docker build", [docker_bin, "build", "-t", state.image, "."], cwd=repo)
        if not _state_step_passed(state, "docker_push"):
            self._run_command(report, state, state_path, "Docker push", [docker_bin, "push", state.image], cwd=repo)
        if not _state_step_passed(state, "image_verify"):
            self._run_command(report, state, state_path, "Image verify", _verify_command(docker_bin, state.image, verify_method), cwd=repo)
        operator = self.operator_factory(state.namespace, manifest_dir)
        if not _state_step_passed(state, "k8s_upgrade"):
            try:
                upgrade_report = operator.upgrade(
                    image=state.image,
                    version=state.version,
                    apply=True,
                    preserve_cluster_config=True,
                    preserve_local_config=False,
                    run_doctor=True,
                    show_status=True,
                    cleanup_failed=True,
                )
            except K8sOperatorError as exc:
                self._mark_state(state, state_path, report, "Kubernetes upgrade", "failed", detail=str(exc))
                raise ReleaseUpgradeError("Kubernetes upgrade failed.", report) from exc
            self._mark_state(
                state,
                state_path,
                report,
                "Kubernetes upgrade",
                "passed",
                command=["agentlab", "k8s", "upgrade", "--image", state.image, "--version", state.version, "--apply"],
                stdout=format_upgrade_report(upgrade_report),
            )
            if getattr(upgrade_report, "image_drift", []):
                state.mark("k8s_upgrade", "failed", state_path)
                report.steps[-1].status = "failed"
                report.steps[-1].detail = "image drift remains"
                raise ReleaseUpgradeError("Kubernetes upgrade reported image drift.", report)
        if not _state_step_passed(state, "status"):
            try:
                cluster_status = operator.status(manifest_dir=manifest_dir)
            except K8sOperatorError as exc:
                self._mark_state(state, state_path, report, "Status", "failed", detail=str(exc))
                raise ReleaseUpgradeError("Kubernetes status failed.", report) from exc
            self._mark_state(
                state,
                state_path,
                report,
                "Status",
                "passed",
                command=["agentlab", "k8s", "status", "--namespace", state.namespace, "--manifest-dir", state.manifest_dir],
                stdout=format_status(cluster_status),
            )
            if _status_has_image_drift(cluster_status):
                state.mark("status", "failed", state_path)
                report.steps[-1].status = "failed"
                report.steps[-1].detail = "image drift remains"
                raise ReleaseUpgradeError("Kubernetes status reported image drift.", report)
        if not _state_step_passed(state, "git_tag_push"):
            self._run_command(report, state, state_path, "Git tag push", ["git", "push", "origin", state.version], cwd=repo)
        state.completed = True
        state.write(state_path)
        return report

    def _add_dry_run_steps(self, report: ReleaseUpgradeReport, *, state: ReleaseState, verify_method: str) -> None:
        docker_bin = state.docker_bin or "docker"
        planned = [
            ("Git tag", "git_tag", ["git", "tag", state.version]),
            ("Docker build", "docker_build", [docker_bin, "build", "-t", state.image, "."]),
            ("Docker push", "docker_push", [docker_bin, "push", state.image]),
            ("Image verify", "image_verify", _verify_command(docker_bin, state.image, verify_method)),
            ("Kubernetes upgrade", "k8s_upgrade", ["agentlab", "k8s", "upgrade", "--image", state.image, "--version", state.version, "--apply"]),
            ("Status", "status", ["agentlab", "k8s", "status", "--namespace", state.namespace, "--manifest-dir", state.manifest_dir]),
            ("Git tag push", "git_tag_push", ["git", "push", "origin", state.version]),
        ]
        for name, key, command in planned:
            if not _state_step_passed(state, key):
                report.steps.append(ReleaseStep(name=name, status="planned", command=command))

    def _run_command(
        self,
        report: ReleaseUpgradeReport,
        state: ReleaseState,
        state_path: Path,
        name: str,
        command: list[str],
        *,
        cwd: Path,
    ) -> ReleaseStep:
        result = self.command_runner.run(command, cwd=cwd)
        status = "passed" if result.returncode == 0 else "failed"
        step = self._mark_state(
            state,
            state_path,
            report,
            name,
            status,
            command=result.args,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        if result.returncode != 0:
            raise ReleaseUpgradeError(f"{name} failed.", report)
        return step

    def _mark_state(
        self,
        state: ReleaseState,
        state_path: Path,
        report: ReleaseUpgradeReport,
        name: str,
        status: str,
        *,
        command: list[str] | None = None,
        detail: str = "",
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> ReleaseStep:
        step = ReleaseStep(name=name, status=status, command=command, detail=detail, stdout=stdout, stderr=stderr, returncode=returncode)
        report.steps.append(step)
        step_key = STATE_STEP_KEYS.get(name)
        if step_key:
            state.mark(step_key, status, state_path)
        return step

    def _local_tag_exists(self, tag: str, *, cwd: Path) -> bool:
        result = self.command_runner.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=cwd)
        return result.returncode == 0

    def _command_stdout_or_unknown(self, command: list[str], *, cwd: Path) -> str:
        result = self.command_runner.run(command, cwd=cwd)
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"


def parse_command_text(value: str | None) -> list[str] | None:
    if not value:
        return None
    return shlex.split(value, posix=os.name != "nt")


def format_release_report(report: ReleaseUpgradeReport) -> str:
    lines = [
        f"AgentLab release {report.workflow}",
        "",
        f"Current version: {report.current_version or 'unknown'}",
        f"New version:     {report.new_version or 'not set'}",
        f"Current image:   {report.current_image or 'unknown'}",
        f"New image:       {report.image}",
        f"Verify method:   {report.verify_image_method}",
        f"Version source:  {report.version_source or 'not applicable'}",
        f"Repo: {report.repo}",
        f"Namespace: {report.namespace}",
        f"Manifest dir: {report.manifest_dir}",
    ]
    if report.state_file:
        lines.append(f"State file: {report.state_file}")
    lines.extend(["", "Steps:"])
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


def _dirty_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for raw_line in porcelain.splitlines():
        if not raw_line.strip():
            continue
        path_text = raw_line[3:] if len(raw_line) > 3 else raw_line
        for item in path_text.split(" -> "):
            normalized = item.strip().strip('"').replace("\\", "/")
            if normalized:
                paths.append(normalized)
    return paths


def _only_generated_dirty(paths: list[str], *, repo: Path, manifest_dir: Path) -> bool:
    try:
        generated = manifest_dir.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        generated = manifest_dir.as_posix()
    generated = generated.rstrip("/")
    if not generated:
        return False
    return all(path == generated or path.startswith(f"{generated}/") for path in paths)


def _status_has_image_drift(status: ClusterStatus) -> bool:
    return any(item.image_drift for item in status.cronjobs) or bool(status.manifest_image_drifts)


def _verify_command(docker_bin: str, image: str, method: str) -> list[str]:
    if method == "pull":
        return [docker_bin, "pull", image]
    return [docker_bin, "manifest", "inspect", image]


def _state_step_passed(state: ReleaseState, step_key: str) -> bool:
    return state.steps.get(step_key) in {"passed", "skipped"}


def _step_status(report: ReleaseUpgradeReport, name: str) -> str:
    step = next((item for item in report.steps if item.name == name), None)
    if step is None:
        return "not run"
    return step.detail or step.status
