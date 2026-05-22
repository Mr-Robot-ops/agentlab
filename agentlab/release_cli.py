from __future__ import annotations

from pathlib import Path

import typer

from agentlab.k8s_operator import DEFAULT_MANIFEST_DIR, DEFAULT_NAMESPACE
from agentlab.release_upgrade import (
    ReleaseUpgradeError,
    ReleaseUpgradeOptions,
    ReleaseUpgrader,
    format_release_report,
    parse_command_text,
)


release_app = typer.Typer(help="Run local AgentLab release and upgrade workflows.")


@release_app.command()
def upgrade(
    image: str = typer.Option(..., "--image", help="Container image tag to build, push, and deploy."),
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    skip_git_pull: bool = typer.Option(False, "--skip-git-pull"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    skip_tests: bool = typer.Option(False, "--skip-tests"),
    test_command: str | None = typer.Option(None, "--test-command", help="Override the test command."),
    skip_build: bool = typer.Option(False, "--skip-build"),
    skip_push: bool = typer.Option(False, "--skip-push"),
    docker_bin: str = typer.Option("docker", "--docker-bin"),
    build_arg: list[str] | None = typer.Option(None, "--build-arg"),
    platform: str | None = typer.Option(None, "--platform"),
    apply: bool = typer.Option(False, "--apply/--no-apply"),
    preserve_cluster_config: bool = typer.Option(False, "--preserve-cluster-config"),
    no_preserve_cluster_config: bool = typer.Option(False, "--no-preserve-cluster-config"),
    preserve_local_config: bool = typer.Option(False, "--preserve-local-config"),
    run_doctor: bool = typer.Option(False, "--run-doctor"),
    no_run_doctor: bool = typer.Option(False, "--no-run-doctor"),
    cleanup_failed: bool = typer.Option(False, "--cleanup-failed"),
    no_cleanup_failed: bool = typer.Option(False, "--no-cleanup-failed"),
    status: bool = typer.Option(False, "--status"),
    no_status: bool = typer.Option(False, "--no-status"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Run the local release upgrade workflow."""
    try:
        options = ReleaseUpgradeOptions(
            image=image,
            repo=repo,
            manifest_dir=manifest_dir,
            namespace=namespace,
            skip_git_pull=skip_git_pull,
            allow_dirty=allow_dirty,
            skip_tests=skip_tests,
            test_command=parse_command_text(test_command),
            skip_build=skip_build,
            skip_push=skip_push,
            docker_bin=docker_bin,
            build_args=build_arg or [],
            platform=platform,
            apply=apply,
            preserve_cluster_config=_effective_optional_flag(
                positive=preserve_cluster_config,
                negative=no_preserve_cluster_config,
                default=None,
                name="preserve-cluster-config",
            ),
            preserve_local_config=preserve_local_config,
            run_doctor=_effective_optional_flag(
                positive=run_doctor,
                negative=no_run_doctor,
                default=None,
                name="run-doctor",
            ),
            cleanup_failed=_effective_optional_flag(
                positive=cleanup_failed,
                negative=no_cleanup_failed,
                default=None,
                name="cleanup-failed",
            ),
            status=_effective_optional_flag(
                positive=status,
                negative=no_status,
                default=None,
                name="status",
            ),
            dry_run=dry_run,
        )
        report = ReleaseUpgrader().run(options)
    except ReleaseUpgradeError as exc:
        typer.echo(format_release_report(exc.report))
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(format_release_report(report))


def _effective_optional_flag(*, positive: bool, negative: bool, default: bool | None, name: str) -> bool | None:
    if positive and negative:
        raise ValueError(f"Choose either --{name} or --no-{name}, not both.")
    if positive:
        return True
    if negative:
        return False
    return default
