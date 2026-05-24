from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

import agentlab.update_cli as update_cli
from agentlab.k8s_operator import ClusterStatus
from agentlab.main import app
from agentlab.release_upgrade import ReleaseCommandResult, ReleaseStep, ReleaseUpgradeError, ReleaseUpgradeReport, ReleaseUpgrader
from agentlab.update_cli import UPDATE_REEXEC_ENV, UpdateError, UpdateOptions, UpdateRunner, format_update_report


class ReexecRequested(RuntimeError):
    pass


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.responses: dict[tuple[str, ...], ReleaseCommandResult | list[ReleaseCommandResult]] = {}

    def respond(self, args: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.responses[tuple(args)] = ReleaseCommandResult(args=args, stdout=stdout, stderr=stderr, returncode=returncode)

    def run(self, args: list[str], *, cwd: Path) -> ReleaseCommandResult:
        self.calls.append((args, cwd))
        configured = self.responses.get(tuple(args), ReleaseCommandResult(args=args))
        if isinstance(configured, list):
            return configured.pop(0) if configured else ReleaseCommandResult(args=args)
        return configured


class FakeReleaseUpgrader:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def run(self, options):
        self.calls.append(options)
        image = "registry/agentlab:0.1.20"
        workflow = options.workflow
        new_version = "v0.1.20"
        steps = [
            ReleaseStep(name="Tests", status="planned"),
            ReleaseStep(name="Docker build", status="planned", command=["docker", "build", "-t", image, "."]),
            ReleaseStep(name="Docker push", status="planned", command=["docker", "push", image]),
            ReleaseStep(name="Image verify", status="planned", command=["docker", "pull", image]),
            ReleaseStep(name="Kubernetes upgrade", status="planned"),
            ReleaseStep(name="Status", status="planned"),
        ]
        if getattr(options, "runtime_build", False):
            image = f"{options.image_repository}:{options.runtime_image_tag}"
            new_version = options.runtime_version
            steps = [
                ReleaseStep(name="Tests", status="planned"),
                ReleaseStep(name="Docker build", status="planned", command=["docker", "build", "-t", image, "."]),
                ReleaseStep(name="Docker push", status="planned", command=["docker", "push", image]),
                ReleaseStep(name="Image verify", status="planned", command=["docker", "pull", image]),
                ReleaseStep(name="Kubernetes upgrade", status="planned"),
                ReleaseStep(name="Status", status="planned"),
            ]
        else:
            if options.tag:
                steps.insert(1, ReleaseStep(name="Git tag", status="planned", command=["git", "tag", new_version]))
            if options.push_tag:
                steps.append(ReleaseStep(name="Git tag push", status="planned", command=["git", "push", "origin", new_version]))
        if options.skip_tests:
            steps[0] = ReleaseStep(name="Tests", status="skipped", detail="--skip-tests")
        return ReleaseUpgradeReport(
            image=image,
            repo=str(options.repo),
            namespace=options.namespace,
            manifest_dir=str(options.manifest_dir),
            workflow=workflow,
            dry_run=options.dry_run,
            current_version="v0.1.19",
            new_version=new_version,
            current_image="registry/agentlab:0.1.19",
            image_repository="registry/agentlab",
            verify_image_method=options.verify_image_method,
            steps=steps,
        )


class FakeReleaseResumer:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def run(self, options):
        self.calls.append(options)
        return ReleaseUpgradeReport(
            image="registry/agentlab:0.1.20",
            repo=str(options.repo),
            namespace="agentlab",
            manifest_dir="deploy/kubernetes/generated",
            workflow="resume",
            dry_run=options.dry_run,
            new_version="v0.1.20",
            verify_image_method=options.verify_image_method or "pull",
            steps=[ReleaseStep(name="Image verify", status="planned", command=["docker", "pull", "registry/agentlab:0.1.20"])],
        )


class FakeStatusOperator:
    def __init__(self, image: str | None = "registry/agentlab:0.1.20", manifest_image: str | None = None) -> None:
        self.image = image
        self.manifest_image = manifest_image
        self.calls: list[object] = []

    def status(self, *, manifest_dir: Path):
        self.calls.append(("status", manifest_dir))
        return ClusterStatus(namespace="agentlab", configmap_image=self.image, manifest_configmap_image=self.manifest_image)


class FakeReexecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command: list[str], env) -> None:
        self.calls.append((command, dict(env)))
        raise ReexecRequested()


def write_agentlab_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agentlab"\n', encoding="utf-8")
    (tmp_path / "agentlab").mkdir()
    (tmp_path / "deploy" / "kubernetes" / "generated").mkdir(parents=True)
    return tmp_path


def configure_git_state(
    runner: FakeCommandRunner,
    *,
    current: str = "head123",
    target: str = "target456",
    merge_base: str = "head123",
    behind: str = "2",
    ahead: str = "0",
    dirty: str = "",
) -> None:
    runner.respond(["git", "status", "--porcelain"], stdout=dirty)
    runner.respond(["git", "fetch", "origin"])
    runner.respond(["git", "rev-parse", "HEAD"], stdout=f"{current}\n")
    runner.respond(["git", "rev-parse", "origin/main"], stdout=f"{target}\n")
    runner.respond(["git", "merge-base", "HEAD", "origin/main"], stdout=f"{merge_base}\n")
    runner.respond(["git", "rev-list", "--count", "HEAD..origin/main"], stdout=f"{behind}\n")
    runner.respond(["git", "rev-list", "--count", "origin/main..HEAD"], stdout=f"{ahead}\n")


def make_update_runner(runner: FakeCommandRunner | None = None) -> tuple[UpdateRunner, FakeCommandRunner, FakeReleaseUpgrader, FakeReleaseResumer]:
    command_runner = runner or FakeCommandRunner()
    upgrader = FakeReleaseUpgrader()
    resumer = FakeReleaseResumer()
    return (
        UpdateRunner(command_runner=command_runner, release_upgrader=upgrader, release_resumer=resumer),
        command_runner,
        upgrader,
        resumer,
    )


def make_update_runner_with_reexecutor(
    reexecutor: FakeReexecutor,
    runner: FakeCommandRunner | None = None,
) -> tuple[UpdateRunner, FakeCommandRunner, FakeReleaseUpgrader, FakeReleaseResumer]:
    command_runner = runner or FakeCommandRunner()
    upgrader = FakeReleaseUpgrader()
    resumer = FakeReleaseResumer()
    return (
        UpdateRunner(command_runner=command_runner, release_upgrader=upgrader, release_resumer=resumer, reexecutor=reexecutor),
        command_runner,
        upgrader,
        resumer,
    )


def test_update_dry_run_does_not_run_git_pull_or_self_install(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, upgrader, _resumer = make_update_runner()
    configure_git_state(runner)

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))

    commands = [call[0] for call in runner.calls]
    assert ["git", "fetch", "origin"] in commands
    assert ["git", "pull", "--ff-only", "origin", "main"] not in commands
    assert [sys.executable, "-m", "pip", "install", "-e", "."] not in commands
    assert upgrader.calls[0].dry_run is True
    assert upgrader.calls[0].runtime_build is True
    assert upgrader.calls[0].version is None
    assert upgrader.calls[0].runtime_version == "commit target4"
    assert upgrader.calls[0].tag is False
    assert upgrader.calls[0].push_tag is False
    assert report.release_report is not None


def test_default_update_dry_run_prints_runtime_plan_without_git_tags(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner, current="old123456789", target="new456789012", merge_base="old123456789", behind="2")

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True, image_repository="registry/agentlab"))
    rendered = format_update_report(report)

    assert "Current HEAD: old123456789" in rendered
    assert "Target HEAD:  new456789012" in rendered
    assert "Git state:    behind origin/main by 2 commits" in rendered
    assert sys.executable in rendered
    assert "-m pip install -e ." in rendered
    assert "Runtime:" in rendered
    assert "Image:         registry/agentlab:new4567" in rendered
    assert "Image repo:    registry/agentlab" in rendered
    assert "Version:       commit new4567" in rendered
    assert "Verify method: pull" in rendered
    assert "Namespace:     agentlab" in rendered
    assert "Manifest dir:  deploy" in rendered
    assert "docker build -t registry/agentlab:new4567 ." in rendered
    assert "docker push registry/agentlab:new4567" in rendered
    assert "docker pull registry/agentlab:new4567" in rendered
    assert "Kubernetes upgrade" in rendered
    assert "Status" in rendered
    assert "git tag" not in rendered
    assert "git push origin" not in rendered


def test_default_update_dry_run_infers_image_repository_from_cluster_status(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    command_runner = FakeCommandRunner()
    configure_git_state(command_runner, current="8f1aff3abcdef", target="8f1aff3abcdef", merge_base="8f1aff3abcdef", behind="0")
    operator = FakeStatusOperator("10.159.21.58:5000/agentlab:0.1.20")
    upgrader = ReleaseUpgrader(command_runner=command_runner, operator_factory=lambda namespace, manifest_dir: operator)
    update_runner = UpdateRunner(command_runner=command_runner, release_upgrader=upgrader, release_resumer=FakeReleaseResumer())

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True))
    rendered = format_update_report(report)

    assert "Image:         10.159.21.58:5000/agentlab:8f1aff3" in rendered
    assert "Version:       commit 8f1aff3" in rendered
    assert "--runtime-version 'commit 8f1aff3'" in rendered
    assert "--version 'commit 8f1aff3'" not in rendered
    assert "git tag" not in rendered
    assert "git push origin" not in rendered


def test_default_update_dry_run_infers_image_repository_from_generated_manifest(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    command_runner = FakeCommandRunner()
    configure_git_state(command_runner, current="8f1aff3abcdef", target="8f1aff3abcdef", merge_base="8f1aff3abcdef", behind="0")
    operator = FakeStatusOperator(None, "generated.local/agentlab:0.1.20")
    upgrader = ReleaseUpgrader(command_runner=command_runner, operator_factory=lambda namespace, manifest_dir: operator)
    update_runner = UpdateRunner(command_runner=command_runner, release_upgrader=upgrader, release_resumer=FakeReleaseResumer())

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True))
    rendered = format_update_report(report)

    assert "Image repo:    generated.local/agentlab" in rendered
    assert "Image:         generated.local/agentlab:8f1aff3" in rendered


def test_default_update_without_image_repository_has_clear_failure(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    command_runner = FakeCommandRunner()
    configure_git_state(command_runner, current="8f1aff3abcdef", target="8f1aff3abcdef", merge_base="8f1aff3abcdef", behind="0")
    operator = FakeStatusOperator(None, None)
    upgrader = ReleaseUpgrader(command_runner=command_runner, operator_factory=lambda namespace, manifest_dir: operator)
    update_runner = UpdateRunner(command_runner=command_runner, release_upgrader=upgrader, release_resumer=FakeReleaseResumer())

    try:
        update_runner.run(UpdateOptions(repo=repo, dry_run=True))
    except ReleaseUpgradeError as exc:
        assert "Unable to infer image repository. Run once with --image-repository REPO." in exc.report.failed_step.detail
    else:
        raise AssertionError("expected image repository inference failure")


def test_update_dry_run_identifies_equal_ahead_and_diverged(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)

    equal_runner, equal_cmd, _upgrader, _resumer = make_update_runner()
    configure_git_state(equal_cmd, current="same", target="same", merge_base="same", behind="0", ahead="0")
    equal_report = equal_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))
    assert equal_report.git_state == "equal to origin/main"

    ahead_runner, ahead_cmd, _upgrader, _resumer = make_update_runner()
    configure_git_state(ahead_cmd, current="local", target="origin", merge_base="origin", behind="0", ahead="1")
    ahead_report = ahead_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))
    assert ahead_report.git_state == "ahead of origin/main by 1 commits"

    diverged_runner, diverged_cmd, _upgrader, _resumer = make_update_runner()
    configure_git_state(diverged_cmd, current="local", target="origin", merge_base="base", behind="1", ahead="1")
    try:
        diverged_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))
    except UpdateError as exc:
        assert "diverged" in exc.report.git_state
    else:
        raise AssertionError("expected diverged update failure")


def test_update_dry_run_no_self_install_marks_step_skipped(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner)

    report = update_runner.run(
        UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab", no_self_install=True)
    )

    assert any(step.name == "Self install" and step.status == "skipped" for step in report.steps)
    assert report.self_install_command is None


def test_update_no_tests_is_passed_to_runtime_deploy(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, upgrader, _resumer = make_update_runner()
    configure_git_state(runner)

    report = update_runner.run(
        UpdateOptions(repo=repo, dry_run=True, image_repository="registry/agentlab", no_tests=True)
    )
    rendered = format_update_report(report)

    assert upgrader.calls[0].skip_tests is True
    assert "Tests: --skip-tests" in rendered


def test_update_resume_dry_run_delegates_to_release_resume(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, _runner, _upgrader, resumer = make_update_runner()

    report = update_runner.run(UpdateOptions(repo=repo, resume=True, dry_run=True, verify_image_method="manifest"))

    assert resumer.calls
    assert resumer.calls[0].dry_run is True
    assert resumer.calls[0].verify_image_method == "manifest"
    assert report.release_report.workflow == "resume"


def test_update_rejects_invalid_options_before_git_or_self_install(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, _upgrader, _resumer = make_update_runner()

    try:
        update_runner.run(UpdateOptions(repo=repo, verify_image_method="digest"))
    except UpdateError as exc:
        assert "Invalid --verify-image-method" in str(exc)
    else:
        raise AssertionError("expected invalid verify method failure")

    assert runner.calls == []

    update_runner, runner, _upgrader, _resumer = make_update_runner()

    try:
        update_runner.run(UpdateOptions(repo=repo, patch=True, minor=True))
    except UpdateError as exc:
        assert "Choose only one version bump mode" in str(exc)
    else:
        raise AssertionError("expected bump mode conflict")

    assert runner.calls == []

    update_runner, runner, _upgrader, _resumer = make_update_runner()
    try:
        update_runner.run(UpdateOptions(repo=repo, push_tag=True, release=True, patch=True))
    except UpdateError as exc:
        assert "--push-tag requires --tag" in str(exc)
    else:
        raise AssertionError("expected push tag without tag failure")

    assert runner.calls == []

    update_runner, runner, _upgrader, _resumer = make_update_runner()
    try:
        update_runner.run(UpdateOptions(repo=repo, patch=True))
    except UpdateError as exc:
        assert "Use --release" in str(exc)
    else:
        raise AssertionError("expected release mode failure")

    assert runner.calls == []


def test_update_allows_generated_manifest_dirtiness(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner, dirty="?? deploy/kubernetes/generated/\n")

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))

    assert report.steps[0].detail == "generated manifests dirty allowed"


def test_explicit_release_update_plans_tags_only_when_requested(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, upgrader, _resumer = make_update_runner()
    configure_git_state(runner, current="same", target="same", merge_base="same", behind="0", ahead="0")

    report = update_runner.run(
        UpdateOptions(
            repo=repo,
            dry_run=True,
            release=True,
            patch=True,
            tag=True,
            current_version="0.1.19",
            image_repository="registry/agentlab",
        )
    )
    rendered = format_update_report(report)

    assert upgrader.calls[0].runtime_build is False
    assert upgrader.calls[0].tag is True
    assert upgrader.calls[0].push_tag is False
    assert "git tag v0.1.20" in rendered
    assert "git push origin v0.1.20" not in rendered

    push_runner, push_cmd, _upgrader, _resumer = make_update_runner()
    configure_git_state(push_cmd, current="same", target="same", merge_base="same", behind="0", ahead="0")
    push_report = push_runner.run(
        UpdateOptions(
            repo=repo,
            dry_run=True,
            release=True,
            patch=True,
            tag=True,
            push_tag=True,
            current_version="0.1.19",
            image_repository="registry/agentlab",
        )
    )

    assert "git push origin v0.1.20" in format_update_report(push_report)


def test_update_rejects_unrelated_dirty_files_unless_allowed(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner, dirty=" M README.md\n")

    try:
        update_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))
    except UpdateError as exc:
        assert exc.report.failed_step.name == "Git status"
    else:
        raise AssertionError("expected dirty update failure")

    allowed_runner, allowed_cmd, _upgrader, _resumer = make_update_runner()
    configure_git_state(allowed_cmd, dirty=" M README.md\n")
    report = allowed_runner.run(
        UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab", allow_dirty=True)
    )
    assert report.steps[0].detail == "dirty allowed"


def test_real_update_runs_pull_self_install_then_reexec_before_release_deploy(tmp_path: Path, monkeypatch) -> None:
    repo = write_agentlab_repo(tmp_path)
    reexecutor = FakeReexecutor()
    update_runner, runner, upgrader, _resumer = make_update_runner_with_reexecutor(reexecutor)
    monkeypatch.setattr(sys, "argv", ["agentlab", "update", "--no-tests"])
    runner.responses[("git", "status", "--porcelain")] = [
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout=""),
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout=""),
    ]

    try:
        update_runner.run(UpdateOptions(repo=repo, image_repository="registry/agentlab", no_tests=True))
    except ReexecRequested:
        pass
    else:
        raise AssertionError("expected update re-exec")

    commands = [call[0] for call in runner.calls]
    assert commands == [
        ["git", "status", "--porcelain"],
        ["git", "pull", "--ff-only", "origin", "main"],
        ["git", "status", "--porcelain"],
        [sys.executable, "-m", "pip", "install", "-e", "."],
    ]
    assert not upgrader.calls
    assert reexecutor.calls
    command, env = reexecutor.calls[0]
    assert command == [sys.executable, "-m", "agentlab.main", "update", "--no-tests"]
    assert env[UPDATE_REEXEC_ENV] == "1"


def test_reexec_marker_skips_pull_and_self_install_then_release_deploy(tmp_path: Path, monkeypatch) -> None:
    repo = write_agentlab_repo(tmp_path)
    reexecutor = FakeReexecutor()
    update_runner, runner, upgrader, _resumer = make_update_runner_with_reexecutor(reexecutor)
    runner.respond(["git", "status", "--porcelain"], stdout="")

    monkeypatch.setenv(UPDATE_REEXEC_ENV, "1")
    report = update_runner.run(UpdateOptions(repo=repo, current_version="0.1.19", image_repository="registry/agentlab"))

    assert [call[0] for call in runner.calls] == [["git", "status", "--porcelain"], ["git", "rev-parse", "--short", "HEAD"]]
    assert not reexecutor.calls
    assert any(step.name == "Git pull" and step.status == "skipped" for step in report.steps)
    assert any(step.name == "Self install" and step.status == "skipped" for step in report.steps)
    assert upgrader.calls[0].workflow == "update-runtime"
    assert upgrader.calls[0].tag is False
    assert upgrader.calls[0].push_tag is False
    assert upgrader.calls[0].skip_git_pull is True
    assert upgrader.calls[0].verify_image_method == "pull"
    assert upgrader.calls[0].push_tag_after_k8s is True


def test_reexec_marker_preserves_no_tests_in_release_options(tmp_path: Path, monkeypatch) -> None:
    repo = write_agentlab_repo(tmp_path)
    update_runner, runner, upgrader, _resumer = make_update_runner()
    runner.respond(["git", "status", "--porcelain"], stdout="")

    monkeypatch.setenv(UPDATE_REEXEC_ENV, "1")
    update_runner.run(UpdateOptions(repo=repo, image_repository="registry/agentlab", no_tests=True))

    assert upgrader.calls[0].skip_tests is True


def test_update_dry_run_resume_and_no_self_install_do_not_reexec(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    reexecutor = FakeReexecutor()

    dry_runner, dry_cmd, _upgrader, _resumer = make_update_runner_with_reexecutor(reexecutor)
    configure_git_state(dry_cmd)
    dry_runner.run(UpdateOptions(repo=repo, dry_run=True, current_version="0.1.19", image_repository="registry/agentlab"))
    assert not reexecutor.calls

    resume_runner, _resume_cmd, _upgrader, resumer = make_update_runner_with_reexecutor(reexecutor)
    resume_runner.run(UpdateOptions(repo=repo, resume=True, dry_run=False, current_version="0.1.19", image_repository="registry/agentlab"))
    assert resumer.calls
    assert not reexecutor.calls

    no_install_runner, no_install_cmd, upgrader, _resumer = make_update_runner_with_reexecutor(reexecutor)
    no_install_cmd.responses[("git", "status", "--porcelain")] = [
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout=""),
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout=""),
    ]
    no_install_runner.run(
        UpdateOptions(repo=repo, current_version="0.1.19", image_repository="registry/agentlab", no_self_install=True)
    )
    assert [sys.executable, "-m", "pip", "install", "-e", "."] not in [call[0] for call in no_install_cmd.calls]
    assert upgrader.calls
    assert not reexecutor.calls


def test_update_auto_clears_pre_deploy_runtime_state(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    state = repo / ".agentlab" / "release-state.json"
    state.parent.mkdir()
    state.write_text(
        '{"completed": false, "workflow": "update-runtime", "steps": {"tests": "failed", "docker_build": "pending", "docker_push": "pending", "k8s_upgrade": "pending"}}',
        encoding="utf-8",
    )
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner)

    report = update_runner.run(UpdateOptions(repo=repo, dry_run=True, image_repository="registry/agentlab"))

    assert not state.exists()
    assert any(step.name == "Release state" and step.detail == "cleared incomplete pre-deploy update state" for step in report.steps)


def test_update_refuses_existing_incomplete_deploy_state_with_clear_hint(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    state = repo / ".agentlab" / "release-state.json"
    state.parent.mkdir()
    state.write_text(
        '{"completed": false, "workflow": "update-runtime", "steps": {"docker_build": "passed", "docker_push": "pending", "k8s_upgrade": "pending"}}',
        encoding="utf-8",
    )
    update_runner, runner, _upgrader, _resumer = make_update_runner()
    configure_git_state(runner)

    try:
        update_runner.run(UpdateOptions(repo=repo, dry_run=True, image_repository="registry/agentlab"))
    except UpdateError as exc:
        assert "update --resume" in str(exc)
        assert "update --clear-state" in str(exc)
    else:
        raise AssertionError("expected incomplete state failure")


def test_update_clear_state_removes_state_without_git_or_release(tmp_path: Path) -> None:
    repo = write_agentlab_repo(tmp_path)
    state = repo / ".agentlab" / "release-state.json"
    state.parent.mkdir()
    state.write_text('{"completed": false}', encoding="utf-8")
    update_runner, runner, upgrader, _resumer = make_update_runner()

    report = update_runner.run(UpdateOptions(repo=repo, clear_state=True))

    assert not state.exists()
    assert any(step.name == "Release state" and step.detail == "cleared" for step in report.steps)
    assert runner.calls == []
    assert upgrader.calls == []


def test_update_cli_is_registered(monkeypatch) -> None:
    class FakeUpdateRunner:
        def run(self, options):
            assert options.dry_run is True
            return update_cli.UpdateReport(repo="repo", dry_run=True)

    monkeypatch.setattr(update_cli, "UpdateRunner", FakeUpdateRunner)

    result = CliRunner().invoke(app, ["update", "--dry-run"])

    assert result.exit_code == 0
    assert "AgentLab update dry-run" in result.output
