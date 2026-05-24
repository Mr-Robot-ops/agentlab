from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

import typer

from agentlab.k8s_operator import DEFAULT_MANIFEST_DIR, DEFAULT_NAMESPACE
from agentlab.release_upgrade import (
    DEFAULT_RELEASE_STATE_FILE,
    ReleaseCommandResult,
    ReleaseCommandRunner,
    ReleaseResumeOptions,
    ReleaseResumer,
    ReleaseStep,
    ReleaseUpgradeError,
    ReleaseUpgradeOptions,
    ReleaseUpgrader,
    SubprocessReleaseCommandRunner,
    VERIFY_IMAGE_METHODS,
    _dirty_paths,
    _only_generated_dirty,
    format_release_report,
)

UPDATE_REEXEC_ENV = "AGENTLAB_UPDATE_REEXEC"


@dataclass
class UpdateOptions:
    repo: Path = field(default_factory=Path.cwd)
    dry_run: bool = False
    resume: bool = False
    patch: bool = False
    minor: bool = False
    major: bool = False
    current_version: str | None = None
    image_repository: str | None = None
    allow_dirty: bool = False
    no_git_pull: bool = False
    no_self_install: bool = False
    verify_image_method: str = "pull"
    namespace: str = DEFAULT_NAMESPACE
    manifest_dir: Path = DEFAULT_MANIFEST_DIR
    state_file: Path = DEFAULT_RELEASE_STATE_FILE
    reexeced: bool = field(default_factory=lambda: os.environ.get(UPDATE_REEXEC_ENV) == "1")

    def resolved_repo(self) -> Path:
        return self.repo.resolve()

    def resolved_state_file(self) -> Path:
        if self.state_file.is_absolute():
            return self.state_file
        return self.resolved_repo() / self.state_file


@dataclass
class UpdateReport:
    repo: str
    dry_run: bool = False
    current_head: str = "unknown"
    target_head: str = "unknown"
    git_state: str = "unknown"
    planned_update: str = "not checked"
    self_install_command: list[str] | None = None
    release_report: object | None = None
    steps: list[ReleaseStep] = field(default_factory=list)

    @property
    def failed_step(self) -> ReleaseStep | None:
        return next((step for step in self.steps if step.status == "failed"), None)


class UpdateError(RuntimeError):
    def __init__(self, message: str, report: UpdateReport) -> None:
        self.report = report
        super().__init__(message)


class UpdateRunner:
    def __init__(
        self,
        *,
        command_runner: ReleaseCommandRunner | None = None,
        release_upgrader: ReleaseUpgrader | None = None,
        release_resumer: ReleaseResumer | None = None,
        reexecutor: Callable[[list[str], Mapping[str, str]], None] | None = None,
    ) -> None:
        self.command_runner = command_runner or SubprocessReleaseCommandRunner()
        self.release_upgrader = release_upgrader or ReleaseUpgrader()
        self.release_resumer = release_resumer or ReleaseResumer()
        self.reexecutor = reexecutor or reexec_update_process

    def run(self, options: UpdateOptions) -> UpdateReport:
        repo = options.resolved_repo()
        report = UpdateReport(repo=str(repo), dry_run=options.dry_run)
        self._validate_repo(repo=repo, report=report)
        self._validate_options(options, report)
        if options.resume:
            resume_report = self.release_resumer.run(
                ReleaseResumeOptions(
                    repo=repo,
                    state_file=options.state_file,
                    verify_image_method=options.verify_image_method,
                    dry_run=options.dry_run,
                )
            )
            report.release_report = resume_report
            return report
        self._check_incomplete_state(options, report)
        self._check_git_status(options, report, "Git status", cwd=repo)
        if options.dry_run:
            self._dry_run(options, report, repo=repo)
            return report
        if options.reexeced:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="already completed before re-exec"))
        elif options.no_git_pull:
            report.steps.append(ReleaseStep(name="Git pull", status="skipped", detail="--no-git-pull"))
        else:
            self._run_command(report, "Git pull", ["git", "pull", "--ff-only", "origin", "main"], cwd=repo)
            self._check_git_status(options, report, "Git status after pull", cwd=repo)
        if options.reexeced:
            report.steps.append(ReleaseStep(name="Self install", status="skipped", detail="already completed before re-exec"))
        elif options.no_self_install:
            report.steps.append(ReleaseStep(name="Self install", status="skipped", detail="--no-self-install"))
        else:
            self._run_command(report, "Self install", self_install_command(), cwd=repo)
            self._reexec_update(report)
        release_report = self.release_upgrader.run(self._release_options(options, dry_run=False, skip_git_pull=True))
        report.release_report = release_report
        return report

    def _dry_run(self, options: UpdateOptions, report: UpdateReport, *, repo: Path) -> None:
        if options.no_git_pull:
            report.steps.append(ReleaseStep(name="Git fetch", status="skipped", detail="--no-git-pull"))
            report.current_head = self._stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=repo)
            report.target_head = report.current_head
            report.git_state = "git pull disabled"
            report.planned_update = "none"
        else:
            self._run_command(report, "Git fetch", ["git", "fetch", "origin"], cwd=repo)
            comparison = self._compare_origin_main(repo)
            report.current_head = comparison.current_head
            report.target_head = comparison.target_head
            report.git_state = comparison.state
            report.planned_update = comparison.planned_update
            if comparison.diverged:
                step = ReleaseStep(name="Git state", status="failed", detail=comparison.state)
                report.steps.append(step)
                raise UpdateError("Local branch diverged from origin/main. Resolve Git history before update.", report)
        if options.no_self_install:
            report.steps.append(ReleaseStep(name="Self install", status="skipped", detail="--no-self-install"))
        else:
            command = self_install_command()
            report.self_install_command = command
            report.steps.append(ReleaseStep(name="Self install", status="planned", command=command))
        release_report = self.release_upgrader.run(self._release_options(options, dry_run=True, skip_git_pull=True))
        report.release_report = release_report

    def _release_options(self, options: UpdateOptions, *, dry_run: bool, skip_git_pull: bool) -> ReleaseUpgradeOptions:
        patch = options.patch
        if not options.patch and not options.minor and not options.major:
            patch = True
        return ReleaseUpgradeOptions(
            current_version=options.current_version,
            bump_patch=patch,
            bump_minor=options.minor,
            bump_major=options.major,
            image_repository=options.image_repository,
            repo=options.resolved_repo(),
            namespace=options.namespace,
            manifest_dir=options.manifest_dir,
            verify_image=True,
            verify_image_method=options.verify_image_method,
            tag=True,
            push_tag=True,
            push_tag_after_k8s=True,
            state_file=None if dry_run else options.state_file,
            workflow="deploy",
            skip_git_pull=skip_git_pull,
            allow_dirty=options.allow_dirty,
            apply=True,
            preserve_cluster_config=True,
            run_doctor=True,
            cleanup_failed=True,
            status=True,
            dry_run=dry_run,
        )

    def _check_incomplete_state(self, options: UpdateOptions, report: UpdateReport) -> None:
        state_path = options.resolved_state_file()
        if not state_path.exists():
            return
        try:
            import json

            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not data.get("completed", False):
            detail = "Existing incomplete release state found. Run `agentlab update --resume`."
            report.steps.append(ReleaseStep(name="Release state", status="failed", detail=detail))
            raise UpdateError(detail, report)

    def _validate_options(self, options: UpdateOptions, report: UpdateReport) -> None:
        if options.verify_image_method not in VERIFY_IMAGE_METHODS:
            detail = "Invalid --verify-image-method. Choose manifest or pull."
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise UpdateError(detail, report)
        selected_bumps = [
            name
            for name, enabled in (
                ("--patch", options.patch),
                ("--minor", options.minor),
                ("--major", options.major),
            )
            if enabled
        ]
        if len(selected_bumps) > 1:
            detail = "Choose only one version bump mode: --patch, --minor, or --major."
            detail += " Selected: " + ", ".join(selected_bumps)
            report.steps.append(ReleaseStep(name="Validate options", status="failed", detail=detail))
            raise UpdateError(detail, report)

    def _validate_repo(self, *, repo: Path, report: UpdateReport) -> None:
        if not repo.exists() or not repo.is_dir():
            detail = f"Repository does not exist: {repo}"
            report.steps.append(ReleaseStep(name="Validate repo", status="failed", detail=detail))
            raise UpdateError(detail, report)
        if not (repo / "pyproject.toml").exists() or not (repo / "agentlab").is_dir():
            detail = f"Repository does not look like AgentLab: {repo}"
            report.steps.append(ReleaseStep(name="Validate repo", status="failed", detail=detail))
            raise UpdateError(detail, report)

    def _check_git_status(self, options: UpdateOptions, report: UpdateReport, name: str, *, cwd: Path) -> ReleaseStep:
        step = self._run_command(report, name, ["git", "status", "--porcelain"], cwd=cwd)
        dirty = _dirty_paths(step.stdout)
        if not dirty:
            step.detail = "clean"
            return step
        manifest_dir = options.manifest_dir if options.manifest_dir.is_absolute() else cwd / options.manifest_dir
        if _only_generated_dirty(dirty, repo=cwd, manifest_dir=manifest_dir):
            step.detail = "generated manifests dirty allowed"
            return step
        if options.allow_dirty:
            step.detail = "dirty allowed"
            return step
        step.status = "failed"
        step.detail = "dirty working tree"
        raise UpdateError("Working tree is dirty. Use --allow-dirty to continue.", report)

    def _compare_origin_main(self, repo: Path) -> "_GitComparison":
        current = self._stdout_or_unknown(["git", "rev-parse", "HEAD"], cwd=repo)
        target = self._stdout_or_unknown(["git", "rev-parse", "origin/main"], cwd=repo)
        merge_base = self._stdout_or_unknown(["git", "merge-base", "HEAD", "origin/main"], cwd=repo)
        behind = self._stdout_or_zero(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=repo)
        ahead = self._stdout_or_zero(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=repo)
        if current == target:
            return _GitComparison(current, target, "equal to origin/main", "none")
        if merge_base == current:
            return _GitComparison(current, target, f"behind origin/main by {behind} commits", "git pull --ff-only origin main")
        if merge_base == target:
            return _GitComparison(current, target, f"ahead of origin/main by {ahead} commits", "none")
        return _GitComparison(current, target, "diverged from origin/main", "none", diverged=True)

    def _run_command(self, report: UpdateReport, name: str, command: list[str], *, cwd: Path) -> ReleaseStep:
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
            raise UpdateError(f"{name} failed.", report)
        return step

    def _stdout_or_unknown(self, command: list[str], *, cwd: Path) -> str:
        result = self.command_runner.run(command, cwd=cwd)
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"

    def _stdout_or_zero(self, command: list[str], *, cwd: Path) -> str:
        result = self.command_runner.run(command, cwd=cwd)
        if result.returncode != 0:
            return "0"
        return result.stdout.strip() or "0"

    def _reexec_update(self, report: UpdateReport) -> None:
        command = reexec_update_command()
        env = dict(os.environ)
        env[UPDATE_REEXEC_ENV] = "1"
        report.steps.append(
            ReleaseStep(
                name="Re-exec update",
                status="handoff",
                command=command,
                detail=f"reload updated code with {UPDATE_REEXEC_ENV}=1",
            )
        )
        self.reexecutor(command, env)


@dataclass
class _GitComparison:
    current_head: str
    target_head: str
    state: str
    planned_update: str
    diverged: bool = False


def self_install_command() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "-e", "."]


def reexec_update_command() -> list[str]:
    return [sys.executable, "-m", "agentlab.main", *sys.argv[1:]]


def reexec_update_process(command: list[str], env: Mapping[str, str]) -> None:
    os.execvpe(command[0], command, dict(env))


def format_update_report(report: UpdateReport) -> str:
    title = "AgentLab update dry-run" if report.dry_run else "AgentLab update"
    lines = [
        title,
        "",
        f"Repo: {report.repo}",
        f"Current HEAD: {report.current_head}",
        f"Target HEAD:  {report.target_head}",
        f"Git state:    {report.git_state}",
        "Planned update:",
        f"  {report.planned_update}",
    ]
    if report.self_install_command:
        lines.extend(["", "Planned self install:", f"  {_format_command(report.self_install_command)}"])
    release_report = report.release_report
    if release_report is not None:
        lines.extend(
            [
                "",
                "Release:",
                f"  Current version: {getattr(release_report, 'current_version', None) or 'unknown'}",
                f"  New version:     {getattr(release_report, 'new_version', None) or 'not set'}",
                f"  Current image:   {getattr(release_report, 'current_image', None) or 'unknown'}",
                f"  New image:       {getattr(release_report, 'image', '')}",
                f"  Image repo:      {getattr(release_report, 'image_repository', None) or 'unknown'}",
                f"  Verify method:   {getattr(release_report, 'verify_image_method', 'unknown')}",
                f"  Namespace:       {getattr(release_report, 'namespace', 'unknown')}",
                f"  Manifest dir:    {getattr(release_report, 'manifest_dir', 'unknown')}",
            ]
        )
    lines.extend(["", "Steps:"])
    combined_steps = list(report.steps)
    if release_report is not None:
        combined_steps.extend(_release_steps_for_update(release_report))
    for index, step in enumerate(combined_steps, start=1):
        status = step.detail or step.status
        lines.append(f"{index}. {step.name}: {status}")
        if step.command:
            lines.append(f"   Command: {_format_command(step.command)}")
    failed = report.failed_step
    if failed is not None:
        lines.extend(["", "Failure:", f"- failed step: {failed.name}"])
        snippet = " ".join((failed.stderr or failed.detail).strip().split())
        if snippet:
            lines.append(f"- stderr: {snippet}")
    return "\n".join(lines)


def _release_steps_for_update(release_report: object) -> list[ReleaseStep]:
    steps: Sequence[ReleaseStep] = getattr(release_report, "steps", [])
    return [
        step
        for step in steps
        if step.name not in {"Git status", "Git pull", "Git status after pull"}
        and not (step.name == "Kubernetes manifest preflight" and step.status == "planned")
    ]


def _format_command(command: list[str]) -> str:
    return shlex.join(str(part) for part in command)


def update(
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    resume: bool = typer.Option(False, "--resume"),
    patch: bool = typer.Option(False, "--patch", help="Bump the patch release version."),
    minor: bool = typer.Option(False, "--minor", help="Bump the minor release version."),
    major: bool = typer.Option(False, "--major", help="Bump the major release version."),
    current_version: str | None = typer.Option(None, "--current-version"),
    image_repository: str | None = typer.Option(None, "--image-repository"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    no_git_pull: bool = typer.Option(False, "--no-git-pull"),
    no_self_install: bool = typer.Option(False, "--no-self-install"),
    verify_image_method: str = typer.Option("pull", "--verify-image-method", help="Image verification method: manifest or pull."),
) -> None:
    """Update AgentLab from origin/main and deploy the next release."""
    try:
        report = UpdateRunner().run(
            UpdateOptions(
                repo=repo,
                dry_run=dry_run,
                resume=resume,
                patch=patch,
                minor=minor,
                major=major,
                current_version=current_version,
                image_repository=image_repository,
                allow_dirty=allow_dirty,
                no_git_pull=no_git_pull,
                no_self_install=no_self_install,
                verify_image_method=verify_image_method,
            )
        )
    except ReleaseUpgradeError as exc:
        typer.echo(format_release_report(exc.report))
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except UpdateError as exc:
        typer.echo(format_update_report(exc.report))
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(format_update_report(report))
