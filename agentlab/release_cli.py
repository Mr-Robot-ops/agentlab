from __future__ import annotations

from pathlib import Path

import typer

from agentlab.k8s_operator import DEFAULT_MANIFEST_DIR, DEFAULT_NAMESPACE
from agentlab.release_upgrade import (
    DEFAULT_RELEASE_STATE_FILE,
    ReleaseResumeOptions,
    ReleaseResumer,
    ReleaseState,
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
    runtime_version: str | None = typer.Option(None, "--runtime-version", help="Runtime version annotation text, for example 'commit 8f1aff3'."),
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
    verify_image_method: str = typer.Option("manifest", "--verify-image-method", help="Image verification method: manifest or pull."),
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
            runtime_version=runtime_version,
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
            verify_image_method=verify_image_method,
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


@release_app.command()
def deploy(
    image: str | None = typer.Option(None, "--image", help="Deploy this explicit image instead of deriving one from a version."),
    version: str | None = typer.Option(None, "--version", help="Explicit release version, for example 0.1.18 or v0.1.18."),
    current_version: str | None = typer.Option(None, "--current-version", help="Override current release version for bump calculations."),
    bump_patch: bool = typer.Option(False, "--bump-patch", help="Bump the patch release version."),
    bump_minor: bool = typer.Option(False, "--bump-minor", help="Bump the minor release version."),
    bump_major: bool = typer.Option(False, "--bump-major", help="Bump the major release version."),
    image_repository: str | None = typer.Option(None, "--image-repository", help="Docker image repository to tag for versioned releases."),
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    verify_image_method: str = typer.Option("pull", "--verify-image-method", help="Image verification method: manifest or pull."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_git_pull: bool = typer.Option(False, "--no-git-pull"),
    no_tests: bool = typer.Option(False, "--no-tests"),
    no_build: bool = typer.Option(False, "--no-build"),
    no_push: bool = typer.Option(False, "--no-push"),
    no_apply: bool = typer.Option(False, "--no-apply"),
    no_tag: bool = typer.Option(False, "--no-tag"),
    no_push_tag: bool = typer.Option(False, "--no-push-tag"),
    no_doctor: bool = typer.Option(False, "--no-doctor"),
    no_cleanup_failed: bool = typer.Option(False, "--no-cleanup-failed"),
    no_status: bool = typer.Option(False, "--no-status"),
) -> None:
    """Run the safe one-command AgentLab release deploy workflow."""
    if not image and not version and not bump_patch and not bump_minor and not bump_major:
        bump_patch = True
    apply = not no_apply
    tag = not no_tag and bool(version or bump_patch or bump_minor or bump_major)
    push_tag = apply and tag and not no_push_tag
    try:
        options = ReleaseUpgradeOptions(
            image=image,
            version=version,
            current_version=current_version,
            bump_patch=bump_patch,
            bump_minor=bump_minor,
            bump_major=bump_major,
            image_repository=image_repository,
            tag=tag,
            push_tag=push_tag,
            verify_image=True,
            verify_image_method=verify_image_method,
            push_tag_after_k8s=True,
            state_file=None if dry_run else DEFAULT_RELEASE_STATE_FILE,
            workflow="deploy",
            repo=repo,
            manifest_dir=manifest_dir,
            namespace=namespace,
            skip_git_pull=no_git_pull,
            skip_tests=no_tests,
            skip_build=no_build,
            skip_push=no_push,
            apply=apply,
            preserve_cluster_config=True,
            run_doctor=not no_doctor,
            cleanup_failed=not no_cleanup_failed,
            status=not no_status,
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


@release_app.command()
def resume(
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    state_file: Path = typer.Option(DEFAULT_RELEASE_STATE_FILE, "--state-file"),
    verify_image_method: str | None = typer.Option(None, "--verify-image-method", help="Override image verification method: manifest or pull."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    allow_head_mismatch: bool = typer.Option(False, "--allow-head-mismatch"),
) -> None:
    """Resume the latest failed or incomplete release from local state."""
    try:
        report = ReleaseResumer().run(
            ReleaseResumeOptions(
                repo=repo,
                state_file=state_file,
                verify_image_method=verify_image_method,
                dry_run=dry_run,
                allow_head_mismatch=allow_head_mismatch,
            )
        )
    except ReleaseUpgradeError as exc:
        typer.echo(format_release_report(exc.report))
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(format_release_report(report))


@release_app.command()
def publish(
    version: str | None = typer.Option(None, "--version", help="Explicit release version, for example 0.1.21 or v0.1.21."),
    current_version: str | None = typer.Option(None, "--current-version", help="Override current release version for bump calculations."),
    bump_patch: bool = typer.Option(False, "--bump-patch", help="Bump the patch release version."),
    bump_minor: bool = typer.Option(False, "--bump-minor", help="Bump the minor release version."),
    bump_major: bool = typer.Option(False, "--bump-major", help="Bump the major release version."),
    image_repository: str | None = typer.Option(None, "--image-repository", help="Docker image repository to tag for versioned releases."),
    tag: bool = typer.Option(False, "--tag/--no-tag", help="Create a local Git tag."),
    push_tag: bool = typer.Option(False, "--push-tag/--no-push-tag", help="Push the Git tag after Kubernetes upgrade/status succeeds."),
    github_release: bool = typer.Option(False, "--github-release/--no-github-release", help="Create a GitHub Release after successful deploy."),
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace"),
    manifest_dir: Path = typer.Option(DEFAULT_MANIFEST_DIR, "--manifest-dir"),
    verify_image_method: str = typer.Option("pull", "--verify-image-method", help="Image verification method: manifest or pull."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_git_pull: bool = typer.Option(False, "--no-git-pull"),
    no_tests: bool = typer.Option(False, "--no-tests"),
    no_build: bool = typer.Option(False, "--no-build"),
    no_push: bool = typer.Option(False, "--no-push"),
    no_apply: bool = typer.Option(False, "--no-apply"),
    no_doctor: bool = typer.Option(False, "--no-doctor"),
    no_cleanup_failed: bool = typer.Option(False, "--no-cleanup-failed"),
    no_status: bool = typer.Option(False, "--no-status"),
) -> None:
    """Publish an explicit versioned AgentLab release."""
    if push_tag and not tag:
        typer.echo("--push-tag requires --tag.", err=True)
        raise typer.Exit(code=2)
    if not version and not bump_patch and not bump_minor and not bump_major:
        typer.echo("Choose --version, --bump-patch, --bump-minor, or --bump-major.", err=True)
        raise typer.Exit(code=2)
    apply = not no_apply
    try:
        options = ReleaseUpgradeOptions(
            version=version,
            current_version=current_version,
            bump_patch=bump_patch,
            bump_minor=bump_minor,
            bump_major=bump_major,
            image_repository=image_repository,
            tag=tag,
            push_tag=push_tag,
            github_release=github_release,
            verify_image=True,
            verify_image_method=verify_image_method,
            push_tag_after_k8s=True,
            state_file=None if dry_run else DEFAULT_RELEASE_STATE_FILE,
            workflow="release-publish",
            repo=repo,
            manifest_dir=manifest_dir,
            namespace=namespace,
            skip_git_pull=no_git_pull,
            skip_tests=no_tests,
            skip_build=no_build,
            skip_push=no_push,
            apply=apply,
            preserve_cluster_config=True,
            run_doctor=not no_doctor,
            cleanup_failed=not no_cleanup_failed,
            status=not no_status,
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


@release_app.command(name="state")
def release_state(
    repo: Path = typer.Option(Path("."), "--repo", help="AgentLab repository path."),
    state_file: Path = typer.Option(DEFAULT_RELEASE_STATE_FILE, "--state-file"),
    clear: bool = typer.Option(False, "--clear", help="Delete the state file."),
    clear_completed: bool = typer.Option(False, "--clear-completed", help="Delete the state file only when completed."),
) -> None:
    """Show or clear local release/update state."""
    path = state_file if state_file.is_absolute() else repo.resolve() / state_file
    typer.echo(f"State file: {path}")
    if not path.exists():
        typer.echo("No release/update state found.")
        return
    state = ReleaseState.from_file(path)
    if clear_completed and not state.completed:
        typer.echo("State is not completed; not clearing.")
        raise typer.Exit(code=1)
    if clear or clear_completed:
        path.unlink()
        typer.echo("State cleared.")
        return
    typer.echo(f"Workflow: {state.workflow}")
    typer.echo(f"Target image: {state.image}")
    typer.echo(f"Target version: {state.version or 'not set'}")
    typer.echo(f"Commit: {state.commit}")
    typer.echo(f"Tag enabled: {'yes' if state.tag_enabled else 'no'}")
    typer.echo(f"Push tag enabled: {'yes' if state.push_tag_enabled else 'no'}")
    typer.echo("Steps:")
    for key, value in state.steps.items():
        typer.echo(f"- {key}: {value}")


def _effective_optional_flag(*, positive: bool, negative: bool, default: bool | None, name: str) -> bool | None:
    if positive and negative:
        raise ValueError(f"Choose either --{name} or --no-{name}, not both.")
    if positive:
        return True
    if negative:
        return False
    return default
