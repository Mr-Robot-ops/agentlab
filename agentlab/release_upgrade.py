from __future__ import annotations

import os
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
        return [self.docker_bin, "manifest", "inspect", self.required_image()]

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
    dry_run: bool = False
    current_version: str | None = None
    new_version: str | None = None
    current_image: str | None = None
    image_repository: str | None = None
    version_source: str = ""
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
            dry_run=options.dry_run,
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

        self._check_git_status(report, options, "Git status", cwd=repo)

        if options.skip_git_pull:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="--skip-git-pull"))
        else:
            self._run_command(report, "Git pull", options.git_pull_command(), cwd=repo)
            self._check_git_status(report, options, "Git status after pull", cwd=repo)

        self._ensure_manifest_dir(options, report, repo=repo, manifest_dir=manifest_dir)

        if options.skip_tests:
            report.steps.append(ReleaseStep(name="Tests", status="skipped", detail="--skip-tests"))
        else:
            self._run_command(report, "Tests", options.pytest_command(), cwd=repo)

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
            return report

        if options.tag:
            self._run_command(report, "Git tag", options.git_tag_command(), cwd=repo)

        if options.skip_build:
            report.steps.append(ReleaseStep(name="Docker build", status="skipped", detail="--skip-build"))
        else:
            self._run_command(report, "Docker build", options.docker_build_command(), cwd=repo)

        if options.skip_push:
            report.steps.append(ReleaseStep(name="Docker push", status="skipped", detail="--skip-push"))
        else:
            self._run_command(report, "Docker push", options.docker_push_command(), cwd=repo)

        if options.effective_verify_image():
            self._run_command(report, "Image verify", options.docker_verify_command(), cwd=repo)

        if options.push_tag:
            self._run_command(report, "Git tag push", options.git_push_tag_command(), cwd=repo)

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

    def _validate_options(self, options: ReleaseUpgradeOptions, report: ReleaseUpgradeReport) -> None:
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
        if options.push_tag:
            report.steps.append(ReleaseStep(name="Git tag push", status="planned", command=options.git_push_tag_command()))
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

    def _check_git_status(self, report: ReleaseUpgradeReport, options: ReleaseUpgradeOptions, name: str, *, cwd: Path) -> ReleaseStep:
        step = self._run_command(report, name, ["git", "status", "--porcelain"], cwd=cwd)
        dirty = _dirty_paths(step.stdout)
        if not dirty:
            step.detail = "clean"
            return step
        if _only_generated_dirty(dirty, repo=cwd, manifest_dir=options.resolved_manifest_dir()) and options.allow_generated_dirty:
            step.detail = "generated manifests dirty allowed"
            return step
        if options.allow_dirty:
            step.detail = "dirty allowed"
            return step
        step.status = "failed"
        step.detail = "dirty working tree"
        raise ReleaseUpgradeError("Working tree is dirty. Use --allow-dirty to continue.", report)

    def _ensure_manifest_dir(
        self,
        options: ReleaseUpgradeOptions,
        report: ReleaseUpgradeReport,
        *,
        repo: Path,
        manifest_dir: Path,
    ) -> None:
        if manifest_dir.exists() and not manifest_dir.is_dir():
            detail = f"Kubernetes manifest path is not a directory: {manifest_dir}."
            report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail))
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        if manifest_dir.exists():
            report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="passed", detail="present"))
            return
        if not options.bootstrap_k8s:
            detail = f"Kubernetes manifest dir is missing: {manifest_dir}. Run bootstrap first or use --bootstrap-k8s."
            report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail))
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        self._run_command(report, "Kubernetes bootstrap", options.bootstrap_command(), cwd=repo)
        if not manifest_dir.exists() or not manifest_dir.is_dir():
            detail = f"Kubernetes manifest dir is still missing after bootstrap: {manifest_dir}."
            report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="failed", detail=detail))
            raise ReleaseUpgradeError("Kubernetes manifest preflight failed.", report)
        report.steps.append(ReleaseStep(name="Kubernetes manifest preflight", status="passed", detail="present after bootstrap"))


def parse_command_text(value: str | None) -> list[str] | None:
    if not value:
        return None
    return shlex.split(value, posix=os.name != "nt")


def format_release_report(report: ReleaseUpgradeReport) -> str:
    lines = [
        "AgentLab release upgrade",
        "",
        f"Current version: {report.current_version or 'unknown'}",
        f"New version:     {report.new_version or 'not set'}",
        f"Current image:   {report.current_image or 'unknown'}",
        f"New image:       {report.image}",
        f"Version source:  {report.version_source or 'not applicable'}",
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


def _step_status(report: ReleaseUpgradeReport, name: str) -> str:
    step = next((item for item in report.steps if item.name == name), None)
    if step is None:
        return "not run"
    return step.detail or step.status
