from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import typer

from agentlab.k8s_operator import (
    CONFIG_SETTING_PATHS,
    DEFAULT_MANIFEST_DIR,
    DEFAULT_NAMESPACE,
    ArtifactNotFoundError,
    ConfigSetReport,
    ConfigValueReport,
    K8sOperator,
    K8sOperatorError,
    TuiUnavailableError,
    format_cleanup_report,
    format_failed_resources,
    format_health,
    format_mrs,
    format_runs,
    format_status,
    format_upgrade_report,
    manifest_for_component,
    run_job_name_for_component,
    run_tui,
)


k8s_app = typer.Typer(help="Operate AgentLab Kubernetes runtime resources.")
config_app = typer.Typer(help="Read and safely update allowed AgentLab ConfigMap settings.")
k8s_app.add_typer(config_app, name="config")

TUI_INSTALL_COMMAND_DISPLAY = "python -m pip install -e '.[tui]'"

LOG_COMPONENT_COMPLETIONS = ("latest", "watch", "plan", "action", "review-comments", "doctor")
RUN_COMPONENT_COMPLETIONS = ("watch", "plan", "action", "review-comments", "doctor", "reset-state")
CRONJOB_COMPLETIONS = ("watch", "plan", "action", "review-comments")
RUN_ID_COMPLETIONS = ("latest",)
ARTIFACT_COMPLETIONS = (
    "review_comment_report.json",
    "auto_approval_report.json",
    "approved_plan.json",
    "plan.json",
    "scheduler_report.json",
    "gate_decision.json",
    "implementation_report.json",
    "docs_check_report.json",
    "test_quality_report.json",
    "structured_proposal.json",
    "structured_proposal_report.json",
    "proposed.diff",
    "diff_stats.json",
    "quality_review.json",
    "security_architecture_review.json",
    "mr_finalization_result.json",
    "functional_test_report.json",
    "raw_patch.diff",
)


def _complete_static(candidates: tuple[str, ...], incomplete: str = "") -> list[str]:
    return [candidate for candidate in candidates if candidate.startswith(incomplete)]


def complete_log_component(incomplete: str = "") -> list[str]:
    return _complete_static(LOG_COMPONENT_COMPLETIONS, incomplete)


def complete_run_component(incomplete: str = "") -> list[str]:
    return _complete_static(RUN_COMPONENT_COMPLETIONS, incomplete)


def complete_cronjob(incomplete: str = "") -> list[str]:
    return _complete_static(CRONJOB_COMPLETIONS, incomplete)


def complete_run_id(incomplete: str = "") -> list[str]:
    return _complete_static(RUN_ID_COMPLETIONS, incomplete)


def complete_artifact(incomplete: str = "") -> list[str]:
    return _complete_static(ARTIFACT_COMPLETIONS, incomplete)


def complete_config_path(incomplete: str = "") -> list[str]:
    return _complete_static(CONFIG_SETTING_PATHS, incomplete)


def _operator(namespace: str, manifest_dir: Path = DEFAULT_MANIFEST_DIR) -> K8sOperator:
    return K8sOperator(namespace=namespace, manifest_dir=manifest_dir)


def _fail(message: str, *, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _questionary_available() -> bool:
    return importlib.util.find_spec("questionary") is not None


def _tui_install_command() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "-e", ".[tui]"]


def _format_config_value(value: object, *, exists: bool = True) -> str:
    if not exists:
        return "<unset>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_config_value_report(report: ConfigValueReport) -> str:
    return f"{report.path}: {_format_config_value(report.value, exists=report.exists)}"


def format_config_set_report(report: ConfigSetReport) -> str:
    return "\n".join(
        [
            report.path,
            f"Before: {_format_config_value(report.before, exists=report.before_exists)}",
            f"After: {_format_config_value(report.after)}",
            f"Changed: {'yes' if report.changed else 'no'}",
        ]
    )


@k8s_app.command("help")
def help_command(ctx: typer.Context) -> None:
    """Show help for AgentLab Kubernetes commands."""
    parent = ctx.parent
    typer.echo(parent.get_help() if parent is not None else ctx.get_help())


@k8s_app.command("tui-check")
def tui_check(
    install: bool = typer.Option(
        False,
        "--install",
        help="Install optional questionary support for arrow-key TUI navigation.",
    ),
) -> None:
    """Check whether optional arrow-key TUI support is available."""
    if _questionary_available():
        typer.echo("Arrow-key TUI support: available")
        return

    typer.echo("Arrow-key TUI support: missing")
    typer.echo(f"Install with: {TUI_INSTALL_COMMAND_DISPLAY}")
    if not install:
        return

    command = _tui_install_command()
    typer.echo(f"Running: {sys.executable} -m pip install -e '.[tui]'")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        _fail(f"questionary install failed with exit code {result.returncode}", code=result.returncode)


@k8s_app.command()
def status(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path | None = typer.Option(None, "--manifest-dir"),
) -> None:
    """Show current AgentLab Kubernetes status."""
    operator = _operator(namespace, manifest_dir or DEFAULT_MANIFEST_DIR)
    typer.echo(format_status(operator.status(manifest_dir=manifest_dir)))


@k8s_app.command()
def mrs(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    state: str = typer.Option("opened", "--state", help="GitLab merge request state to list."),
    label: str = typer.Option("agent/generated", "--label", help="Required merge request label."),
    secret_name: str = typer.Option("agentlab-secrets", "--secret-name", help="Kubernetes Secret containing the GitLab token."),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List AgentLab-generated GitLab merge requests."""
    try:
        report = _operator(namespace).mrs(state=state, label=label, secret_name=secret_name)
    except K8sOperatorError as exc:
        _fail(str(exc))
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "namespace": report.namespace,
                    "state": report.state,
                    "label": report.label,
                    "merge_requests": report.merge_requests,
                },
                indent=2,
            )
        )
        return
    typer.echo(format_mrs(report))


@k8s_app.command()
def health(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path | None = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    pvc: str = typer.Option("agentlab-runs", "--pvc"),
    shell_pod: str = typer.Option("artifact-shell", "--shell-pod"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Show a compact AgentLab runtime health summary."""
    try:
        report = _operator(namespace, manifest_dir or DEFAULT_MANIFEST_DIR).health(
            manifest_dir=manifest_dir,
            pvc=pvc,
            shell_pod=shell_pod,
        )
    except K8sOperatorError as exc:
        _fail(str(exc))
    if json_output:
        typer.echo(json.dumps(asdict(report), indent=2))
        return
    typer.echo(format_health(report))


@k8s_app.command()
def logs(
    component: str = typer.Argument(..., autocompletion=complete_log_component),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    follow: bool = typer.Option(True, "--follow/--no-follow"),
    tail: int | None = typer.Option(None, "--tail", min=1),
) -> None:
    """Show logs for the latest matching AgentLab Job."""
    operator = _operator(namespace)
    try:
        job_name = operator.latest_job_name(component)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(f"Selected Job: {job_name}")
    try:
        output = operator.job_logs(job_name, follow=follow, tail=tail)
    except K8sOperatorError as exc:
        _fail(str(exc))
    if output:
        typer.echo(output)


@config_app.command("get")
def config_get(
    path: str = typer.Argument(..., autocompletion=complete_config_path),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
) -> None:
    """Read an allowed AgentLab ConfigMap setting."""
    try:
        report = _operator(namespace).config_get(path)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(format_config_value_report(report))


@config_app.command("set")
def config_set(
    path: str = typer.Argument(..., autocompletion=complete_config_path),
    value: str = typer.Argument(...),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
) -> None:
    """Update an allowed AgentLab ConfigMap setting."""
    try:
        report = _operator(namespace).config_set(path, value)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(format_config_set_report(report))


@k8s_app.command()
def run(
    component: str = typer.Argument(..., autocompletion=complete_run_component),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    follow: bool = typer.Option(True, "--follow/--no-follow"),
    task_id: str | None = typer.Option(None, "--task-id", help="Run a specific approved scheduler task ID. Only valid with action."),
) -> None:
    """Run a generated AgentLab Kubernetes Job manifest."""
    operator = _operator(namespace, manifest_dir)
    try:
        manifest = manifest_for_component(component, manifest_dir)
        job_name = run_job_name_for_component(component)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(f"Manifest: {manifest}")
    try:
        operator.run_component(component, follow=False, task_id=task_id)
        if follow:
            operator.job_logs(job_name, follow=True)
    except K8sOperatorError as exc:
        _fail(str(exc))


@k8s_app.command()
def artifact(
    run_id: str = typer.Argument(..., autocompletion=complete_run_id),
    artifact_name: str = typer.Argument(..., metavar="ARTIFACT", autocompletion=complete_artifact),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    pvc: str = typer.Option("agentlab-runs", "--pvc"),
    shell_pod: str = typer.Option("artifact-shell", "--shell-pod"),
) -> None:
    """Print an AgentLab run artifact from the runs PVC."""
    try:
        result = _operator(namespace).artifact(run_id, artifact_name, pvc=pvc, shell_pod=shell_pod)
    except ArtifactNotFoundError as exc:
        typer.echo(exc.path, err=True)
        typer.echo("Available artifacts:", err=True)
        if exc.available_artifacts:
            for available in exc.available_artifacts:
                typer.echo(f"- {available}", err=True)
        else:
            typer.echo("- none", err=True)
        raise typer.Exit(code=1) from exc
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(result.path)
    if result.content:
        typer.echo(result.content)


@k8s_app.command()
def runs(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """List recent AgentLab run directories from the runs PVC."""
    try:
        typer.echo(format_runs(_operator(namespace).runs(limit=limit)))
    except K8sOperatorError as exc:
        _fail(str(exc))


@k8s_app.command()
def shell(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    pvc: str = typer.Option("agentlab-runs", "--pvc"),
    shell_pod: str = typer.Option("artifact-shell", "--shell-pod"),
) -> None:
    """Open an interactive shell in the artifact-shell pod."""
    try:
        code = _operator(namespace).shell(pvc=pvc, shell_pod=shell_pod)
    except K8sOperatorError as exc:
        _fail(str(exc))
    if code:
        raise typer.Exit(code=code)


@k8s_app.command("reset-state")
def reset_state(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
) -> None:
    """Run the generated scheduler reset-state Job."""
    operator = _operator(namespace, manifest_dir)
    try:
        manifest = manifest_for_component("reset-state", manifest_dir)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(f"Manifest: {manifest}")
    try:
        operator.run_component("reset-state", follow=False)
        operator.job_logs(run_job_name_for_component("reset-state"), follow=True)
    except K8sOperatorError as exc:
        _fail(str(exc))


@k8s_app.command("cleanup-failed")
def cleanup_failed(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    yes: bool = typer.Option(False, "--yes", help="Delete without prompting for confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show resources that would be deleted without deleting them."),
) -> None:
    """Delete failed AgentLab Jobs and Pods from the namespace."""
    operator = _operator(namespace)
    try:
        resources = operator.failed_resources()
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(format_failed_resources(resources, namespace=namespace))
    if not resources.found:
        return
    if dry_run:
        try:
            typer.echo("")
            typer.echo(format_cleanup_report(operator.cleanup_failed(dry_run=True)))
        except K8sOperatorError as exc:
            _fail(str(exc))
        return
    if not yes and not typer.confirm("Delete these resources?", default=False):
        typer.echo("Cleanup cancelled.")
        return
    try:
        typer.echo("")
        typer.echo(format_cleanup_report(operator.cleanup_failed(dry_run=False)))
    except K8sOperatorError as exc:
        _fail(str(exc))


@k8s_app.command()
def upgrade(
    image: str = typer.Option(..., "--image", help="New AgentLab container image for generated manifests."),
    version: str | None = typer.Option(None, "--version", help="AgentLab release version to write to generated manifests."),
    runtime_version: str | None = typer.Option(None, "--runtime-version", help="Runtime version annotation text to write to generated manifests."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    apply: bool = typer.Option(False, "--apply/--no-apply"),
    preserve_cluster_config: bool = typer.Option(False, "--preserve-cluster-config"),
    preserve_local_config: bool = typer.Option(False, "--preserve-local-config"),
    run_doctor: bool = typer.Option(False, "--run-doctor"),
    status: bool = typer.Option(False, "--status"),
    cleanup_failed: bool = typer.Option(False, "--cleanup-failed"),
    yes: bool = typer.Option(False, "--yes", help="Skip apply confirmation when --apply is set."),
) -> None:
    """Upgrade generated AgentLab Kubernetes manifests to a new image."""
    if version and runtime_version:
        _fail("Choose either --version or --runtime-version, not both.")
    operator = _operator(namespace, manifest_dir)
    if apply and not yes:
        typer.echo(
            "AgentLab Kubernetes upgrade will update generated manifests and apply them to the cluster."
        )
        if not typer.confirm("Continue?", default=False):
            typer.echo("Upgrade cancelled.")
            return
    try:
        kwargs = {
            "image": image,
            "apply": apply,
            "preserve_cluster_config": preserve_cluster_config,
            "preserve_local_config": preserve_local_config,
            "run_doctor": run_doctor,
            "show_status": status,
            "cleanup_failed": cleanup_failed,
        }
        if version or runtime_version:
            kwargs["version"] = version or runtime_version
        report = operator.upgrade(**kwargs)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(format_upgrade_report(report))


@k8s_app.command()
def suspend(
    cronjob: str = typer.Argument(..., autocompletion=complete_cronjob),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
) -> None:
    """Suspend an AgentLab scheduler CronJob."""
    try:
        status = _operator(namespace).set_cronjob_suspend(cronjob, True)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(f"{status.name}: suspend={str(status.suspend).lower()}, active={status.active}, last={status.last_schedule}")


@k8s_app.command()
def resume(
    cronjob: str = typer.Argument(..., autocompletion=complete_cronjob),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
) -> None:
    """Resume an AgentLab scheduler CronJob."""
    try:
        status = _operator(namespace).set_cronjob_suspend(cronjob, False)
    except K8sOperatorError as exc:
        _fail(str(exc))
    typer.echo(f"{status.name}: suspend={str(status.suspend).lower()}, active={status.active}, last={status.last_schedule}")


@k8s_app.command()
def tui(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
) -> None:
    """Open the interactive AgentLab Kubernetes operator TUI."""
    try:
        run_tui(_operator(namespace, manifest_dir))
    except TuiUnavailableError as exc:
        _fail(str(exc), code=2)
    except K8sOperatorError as exc:
        _fail(str(exc))
