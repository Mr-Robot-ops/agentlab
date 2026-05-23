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
    image: str | None = typer.Option(None, "--image", help="Container image tag to build, push, and deploy."),
    version: str | None = typer.Option(None, "--version", help="Explicit release version, for example 0.1.18 or v0.1.18."),
    current_version: str | None = typer.Option(None, "--current-version", help="Override current release version for bump calculations."),
    bump_patch: bool = typer.Option(False, "--bump-patch", help="Bump the patch release version."),
    bump_minor: bool = typer.Option(False, "--bump-minor", help="Bump the minor release version."),
    bump_major: bool = typer.Option(False, "--bump-major", help="Bump the major release version."),
    image_repository: str | None = typer.Option(None, "--image-repository", help="Docker image repository to tag for versioned releases."),
    image_tag_prefix_v: bool = typer.Option(False, "--image-tag-prefix-v", help="Use v-prefixed Docker image tags."),
    tag: bool = typer.Option(False, "--tag/--no-tag", help="Create a local Git tag for versioned releases."),
    push_tag: bool = typer.Option(False, "--push-tag/--no-push-tag", help="Push the Git tag after image push and verification."),
    github_release: bool = typer.Option(False, "--github-release/--no-github-release", help="Create a GitHub Release after successful deploy."),
    verify_image: bool = typer.Option(False, "--verify-image", help="Verify the pushed image before Kubernetes upgrade."),
    no_verify_image: bool = typer.Option(False, "--no-verify-image", help="Skip image verification."),
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    skip_git_pull: bool = typer.Option(False, "--skip-git-pull"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    allow_generated_dirty: bool = typer.Option(True, "--allow-generated-dirty/--no-allow-generated-dirty"),
    pull_ff_only: bool = typer.Option(True, "--pull-ff-only/--no-pull-ff-only"),
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
    prepare_only: bool = typer.Option(False, "--prepare-only"),
    bootstrap_k8s: bool = typer.Option(False, "--bootstrap-k8s"),
    gitlab_url: str | None = typer.Option(None, "--gitlab-url"),
    project_id: str | None = typer.Option(None, "--project-id"),
    target_repo_url: str | None = typer.Option(None, "--target-repo-url"),
    target_repo_ref: str = typer.Option("main", "--target-repo-ref"),
    ollama_url: str | None = typer.Option(None, "--ollama-url"),
    model: str = typer.Option("qwen3.6:35b", "--model"),
    git_author_name: str = typer.Option("AgentLab Bot", "--git-author-name"),
    git_author_email: str = typer.Option("agentlab-bot@example.local", "--git-author-email"),
    mode: str = typer.Option("safe-dry-run", "--mode"),
    schedule_enabled: bool = typer.Option(False, "--schedule-enabled/--no-schedule-enabled"),
) -> None:
    """Run the local release upgrade workflow."""
    try:
        options = ReleaseUpgradeOptions(
            image=image,
            version=version,
            current_version=current_version,
            bump_patch=bump_patch,
            bump_minor=bump_minor,
            bump_major=bump_major,
            image_repository=image_repository,
            image_tag_prefix_v=image_tag_prefix_v,
            tag=tag,
            push_tag=push_tag,
            github_release=github_release,
            verify_image=_effective_optional_flag(
                positive=verify_image,
                negative=no_verify_image,
                default=None,
                name="verify-image",
            ),
            repo=repo,
            manifest_dir=manifest_dir,
            namespace=namespace,
            skip_git_pull=skip_git_pull,
            allow_dirty=allow_dirty,
            allow_generated_dirty=allow_generated_dirty,
            pull_ff_only=pull_ff_only,
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
            prepare_only=prepare_only,
            bootstrap_k8s=bootstrap_k8s,
            gitlab_url=gitlab_url,
            project_id=project_id,
            target_repo_url=target_repo_url,
            target_repo_ref=target_repo_ref,
            ollama_url=ollama_url,
            model=model,
            git_author_name=git_author_name,
            git_author_email=git_author_email,
            mode=mode,
            schedule_enabled=schedule_enabled,
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
