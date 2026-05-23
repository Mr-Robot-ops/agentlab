from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from agentlab.k8s_operator import ClusterStatus
from agentlab.main import app
import agentlab.release_upgrade as release_upgrade
from agentlab.release_upgrade import (
    ReleaseCommandResult,
    ReleaseUpgradeError,
    ReleaseUpgradeOptions,
    ReleaseVersion,
    ReleaseUpgrader,
    bump_release_version,
    format_release_report,
    image_repository_from_image,
    parse_release_version,
)


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.responses: dict[tuple[str, ...], ReleaseCommandResult | list[ReleaseCommandResult]] = {}
        self.side_effects: dict[tuple[str, ...], object] = {}

    def respond(self, args: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.responses[tuple(args)] = ReleaseCommandResult(args=args, stdout=stdout, stderr=stderr, returncode=returncode)

    def on_run(self, args: list[str], callback: object) -> None:
        self.side_effects[tuple(args)] = callback

    def run(self, args: list[str], *, cwd: Path) -> ReleaseCommandResult:
        self.calls.append((args, cwd))
        callback = self.side_effects.get(tuple(args))
        if callable(callback):
            callback()
        configured = self.responses.get(tuple(args), ReleaseCommandResult(args=args))
        if isinstance(configured, list):
            return configured.pop(0) if configured else ReleaseCommandResult(args=args)
        return configured


class FakeK8sOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.upgrade_image_drift: list[str] = []
        self.status_image_drift = False
        self.doctor_status = "completed"
        self.status_configmap_image = "registry/agentlab:new"
        self.status_configmap_version: str | None = None
        self.status_image_annotation_warning: str | None = None

    def upgrade(self, **kwargs):
        self.calls.append(("upgrade", kwargs))
        return type(
            "Upgrade",
            (),
            {
                "namespace": "agentlab",
                "manifest_dir": "deploy/kubernetes/generated",
                "image": kwargs["image"],
                "version": kwargs.get("version"),
                "updated_manifests": ["configmap.yaml"],
                "preserved_sections": ["schedule.review_comments"],
                "apply": kwargs.get("apply", False),
                "applied": kwargs.get("apply", False),
                "run_doctor": kwargs.get("run_doctor", False),
                "doctor_status": self.doctor_status if kwargs.get("run_doctor", False) else "not requested",
                "cleanup_failed": kwargs.get("cleanup_failed", False),
                "cleanup_report": None,
                "status_checked": kwargs.get("show_status", False),
                "image_drift": self.upgrade_image_drift,
            },
        )()

    def status(self, *, manifest_dir: Path):
        self.calls.append(("status", manifest_dir))
        return ClusterStatus(
            namespace="agentlab",
            configmap_image=self.status_configmap_image,
            configmap_version=self.status_configmap_version,
            image_annotation_warning=self.status_image_annotation_warning,
        )


def make_upgrader(fake_operator: FakeK8sOperator | None = None) -> tuple[ReleaseUpgrader, FakeCommandRunner, FakeK8sOperator]:
    runner = FakeCommandRunner()
    operator = fake_operator or FakeK8sOperator()
    upgrader = ReleaseUpgrader(command_runner=runner, operator_factory=lambda namespace, manifest_dir: operator)
    return upgrader, runner, operator


def options(tmp_path: Path, **updates) -> ReleaseUpgradeOptions:
    create_manifest_dir = bool(updates.pop("create_manifest_dir", True))
    if create_manifest_dir:
        (tmp_path / "deploy" / "kubernetes" / "generated").mkdir(parents=True, exist_ok=True)
    values = {
        "image": "registry/agentlab:new",
        "repo": tmp_path,
        "skip_git_pull": True,
        "skip_tests": True,
        "skip_build": True,
        "skip_push": True,
    }
    values.update(updates)
    return ReleaseUpgradeOptions(**values)


def test_release_version_parsing_and_rejection() -> None:
    assert parse_release_version("v0.1.17") == ReleaseVersion(0, 1, 17)
    assert parse_release_version("0.1.17") == ReleaseVersion(0, 1, 17)
    for value in ("latest", "dev", "main"):
        try:
            parse_release_version(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid version: {value}")


def test_release_version_bumps() -> None:
    current = ReleaseVersion(0, 1, 17)

    assert bump_release_version(current, part="patch").tag == "v0.1.18"
    assert bump_release_version(current, part="minor").tag == "v0.2.0"
    assert bump_release_version(current, part="major").tag == "v1.0.0"


def test_image_repository_is_inferred_from_image() -> None:
    assert image_repository_from_image("10.159.21.58:5000/agentlab:0.1.17") == "10.159.21.58:5000/agentlab"


def test_bump_patch_resolves_latest_git_tag_and_deployed_image_repo(tmp_path: Path) -> None:
    operator = FakeK8sOperator()
    operator.status_configmap_image = "10.159.21.58:5000/agentlab:0.1.17"
    upgrader, runner, _operator = make_upgrader(operator)
    runner.respond(["git", "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*"], stdout="v0.1.16\nv0.1.17\n")

    report = upgrader.run(options(tmp_path, image=None, bump_patch=True, dry_run=True))

    assert report.current_version == "v0.1.17"
    assert report.new_version == "v0.1.18"
    assert report.current_image == "10.159.21.58:5000/agentlab:0.1.17"
    assert report.image == "10.159.21.58:5000/agentlab:0.1.18"
    assert report.version_source == "git tag"
    rendered = format_release_report(report)
    assert "Current version: v0.1.17" in rendered
    assert "New version:     v0.1.18" in rendered
    assert "Current image:   10.159.21.58:5000/agentlab:0.1.17" in rendered
    assert "New image:       10.159.21.58:5000/agentlab:0.1.18" in rendered


def test_bump_patch_falls_back_to_kubernetes_version_annotation(tmp_path: Path) -> None:
    operator = FakeK8sOperator()
    operator.status_configmap_version = "v0.1.17"
    operator.status_configmap_image = "registry/agentlab:0.1.16"
    upgrader, _runner, _operator = make_upgrader(operator)

    report = upgrader.run(options(tmp_path, image=None, bump_patch=True, dry_run=True))

    assert report.current_version == "v0.1.17"
    assert report.new_version == "v0.1.18"
    assert report.image == "registry/agentlab:0.1.18"
    assert report.version_source == "version annotation"


def test_bump_patch_falls_back_to_image_annotation(tmp_path: Path) -> None:
    operator = FakeK8sOperator()
    operator.status_configmap_image = "registry/agentlab:0.1.17"
    upgrader, _runner, _operator = make_upgrader(operator)

    report = upgrader.run(options(tmp_path, image=None, bump_patch=True, dry_run=True))

    assert report.current_version == "v0.1.17"
    assert report.image == "registry/agentlab:0.1.18"
    assert report.version_source == "image annotation"


def test_bump_patch_falls_back_to_deprecated_image_annotation(tmp_path: Path) -> None:
    operator = FakeK8sOperator()
    operator.status_configmap_image = "registry/agentlab:0.1.17"
    operator.status_image_annotation_warning = "deprecated image annotation"
    upgrader, _runner, _operator = make_upgrader(operator)

    report = upgrader.run(options(tmp_path, image=None, bump_patch=True, dry_run=True))

    assert report.current_version == "v0.1.17"
    assert report.image == "registry/agentlab:0.1.18"
    assert report.version_source == "deprecated image annotation"


def test_bump_patch_without_source_reports_resolution_failure(tmp_path: Path) -> None:
    class MissingClusterOperator(FakeK8sOperator):
        def status(self, *, manifest_dir: Path):
            raise FileNotFoundError("kubectl")

    upgrader, _runner, _operator = make_upgrader(MissingClusterOperator())

    try:
        upgrader.run(options(tmp_path, image=None, bump_patch=True, dry_run=True))
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected release resolution failure")

    assert report.failed_step is not None
    assert report.failed_step.name == "Resolve release"
    assert "Unable to resolve current release version" in report.failed_step.detail


def test_version_uses_image_repository_override(tmp_path: Path) -> None:
    upgrader, _runner, _operator = make_upgrader()

    report = upgrader.run(
        options(
            tmp_path,
            image=None,
            version="0.1.18",
            image_repository="override/agentlab",
            dry_run=True,
        )
    )

    assert report.new_version == "v0.1.18"
    assert report.image == "override/agentlab:0.1.18"


def test_version_selection_conflicts_fail(tmp_path: Path) -> None:
    for updates in (
        {"image": "registry/agentlab:new", "bump_patch": True},
        {"image": None, "version": "0.1.18", "bump_patch": True},
    ):
        upgrader, _runner, _operator = make_upgrader()
        try:
            upgrader.run(options(tmp_path, dry_run=True, **updates))
        except ReleaseUpgradeError as exc:
            assert "cannot be combined" in str(exc)
        else:
            raise AssertionError("expected version selection conflict")


def test_image_with_explicit_version_writes_release_annotation(tmp_path: Path) -> None:
    upgrader, _runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, image="registry/agentlab:0.1.18", version="0.1.18", verify_image=False))

    assert report.current_version is None
    assert report.new_version == "v0.1.18"
    assert report.current_image is None
    assert report.image == "registry/agentlab:0.1.18"
    assert report.version_source == "--image + --version"
    assert operator.calls[0] == (
        "upgrade",
        {
            "image": "registry/agentlab:0.1.18",
            "version": "v0.1.18",
            "apply": False,
            "preserve_cluster_config": False,
            "preserve_local_config": False,
            "run_doctor": False,
            "show_status": False,
            "cleanup_failed": False,
        },
    )


def test_image_without_version_does_not_invent_release_version(tmp_path: Path) -> None:
    upgrader, _runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, image="registry/agentlab:0.1.18"))

    assert report.new_version is None
    assert "version" not in operator.calls[0][1]


def test_image_with_explicit_version_dry_run_passes_version_to_k8s_upgrade(tmp_path: Path) -> None:
    upgrader, _runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, image="registry/agentlab:0.1.18", version="v0.1.18", dry_run=True))
    rendered = format_release_report(report)

    assert operator.calls == []
    assert "New version:     v0.1.18" in rendered
    assert "New image:       registry/agentlab:0.1.18" in rendered
    assert "agentlab k8s upgrade --image registry/agentlab:0.1.18" in rendered
    assert "--version v0.1.18" in rendered


def test_dry_run_prints_planned_commands_and_executes_nothing(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, dry_run=True, skip_git_pull=False, skip_tests=False, skip_build=False, skip_push=False))
    rendered = format_release_report(report)

    assert runner.calls == []
    assert operator.calls == []
    assert "git pull --ff-only" in rendered
    assert "docker build -t registry/agentlab:new ." in rendered
    assert "agentlab k8s upgrade" in rendered
    assert next(step.command for step in report.steps if step.name == "Tests") == [sys.executable, "-m", "pytest"]
    assert ["python3", "-m", "pytest"] not in [step.command for step in report.steps]


def test_dirty_repo_without_allow_dirty_fails_before_tests_or_build(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["git", "status", "--porcelain"], stdout=" M README.md\n")

    try:
        upgrader.run(options(tmp_path, skip_tests=False, skip_build=False, allow_dirty=False))
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected dirty repository failure")

    assert [call[0] for call in runner.calls] == [["git", "status", "--porcelain"]]
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Git status"


def test_allow_dirty_allows_continuation(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["git", "status", "--porcelain"], stdout=" M README.md\n")

    report = upgrader.run(options(tmp_path, allow_dirty=True))

    assert report.steps[0].detail == "dirty allowed"
    assert operator.calls[0][0] == "upgrade"


def test_test_failure_stops_before_docker_build(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond([sys.executable, "-m", "pytest"], stderr="tests failed", returncode=1)

    try:
        upgrader.run(options(tmp_path, skip_tests=False, skip_build=False))
    except ReleaseUpgradeError:
        pass
    else:
        raise AssertionError("expected test failure")

    commands = [call[0] for call in runner.calls]
    assert [sys.executable, "-m", "pytest"] in commands
    assert ["docker", "build", "-t", "registry/agentlab:new", "."] not in commands
    assert operator.calls == []


def test_docker_build_failure_stops_before_push(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["docker", "build", "-t", "registry/agentlab:new", "."], stderr="build failed", returncode=1)

    try:
        upgrader.run(options(tmp_path, skip_build=False, skip_push=False))
    except ReleaseUpgradeError:
        pass
    else:
        raise AssertionError("expected build failure")

    commands = [call[0] for call in runner.calls]
    assert ["docker", "build", "-t", "registry/agentlab:new", "."] in commands
    assert ["docker", "push", "registry/agentlab:new"] not in commands
    assert operator.calls == []


def test_docker_push_failure_stops_before_k8s_upgrade(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["docker", "push", "registry/agentlab:new"], stderr="push failed", returncode=1)

    try:
        upgrader.run(options(tmp_path, skip_push=False))
    except ReleaseUpgradeError:
        pass
    else:
        raise AssertionError("expected push failure")

    assert ["docker", "push", "registry/agentlab:new"] in [call[0] for call in runner.calls]
    assert operator.calls == []


def test_bump_release_runs_tests_tag_build_push_verify_then_k8s(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(
        options(
            tmp_path,
            image=None,
            current_version="0.1.17",
            bump_patch=True,
            image_repository="registry/agentlab",
            skip_git_pull=True,
            skip_tests=False,
            skip_build=False,
            skip_push=False,
            tag=True,
            push_tag=True,
            apply=True,
        )
    )

    assert [call[0] for call in runner.calls] == [
        ["git", "status", "--porcelain"],
        [sys.executable, "-m", "pytest"],
        ["git", "tag", "v0.1.18"],
        ["docker", "build", "-t", "registry/agentlab:0.1.18", "."],
        ["docker", "push", "registry/agentlab:0.1.18"],
        ["docker", "manifest", "inspect", "registry/agentlab:0.1.18"],
        ["git", "push", "origin", "v0.1.18"],
    ]
    assert operator.calls[0] == (
        "upgrade",
        {
            "image": "registry/agentlab:0.1.18",
            "version": "v0.1.18",
            "apply": True,
            "preserve_cluster_config": True,
            "preserve_local_config": False,
            "run_doctor": True,
            "show_status": True,
            "cleanup_failed": True,
        },
    )
    assert report.failed_step is None


def test_bump_release_without_tag_does_not_create_or_push_git_tag(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(
        options(
            tmp_path,
            image=None,
            current_version="0.1.17",
            bump_patch=True,
            image_repository="registry/agentlab",
            verify_image=False,
        )
    )

    commands = [call[0] for call in runner.calls]
    assert ["git", "tag", "v0.1.18"] not in commands
    assert ["git", "push", "origin", "v0.1.18"] not in commands
    assert operator.calls[0][0] == "upgrade"
    assert report.new_version == "v0.1.18"


def test_tag_is_not_pushed_if_docker_push_fails(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["docker", "push", "registry/agentlab:0.1.18"], stderr="push failed", returncode=1)

    try:
        upgrader.run(
            options(
                tmp_path,
                image=None,
                current_version="0.1.17",
                bump_patch=True,
                image_repository="registry/agentlab",
                skip_build=True,
                skip_push=False,
                tag=True,
                push_tag=True,
            )
        )
    except ReleaseUpgradeError:
        pass
    else:
        raise AssertionError("expected docker push failure")

    commands = [call[0] for call in runner.calls]
    assert ["git", "tag", "v0.1.18"] in commands
    assert ["git", "push", "origin", "v0.1.18"] not in commands
    assert operator.calls == []


def test_image_verify_failure_stops_before_k8s_upgrade(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.respond(["docker", "manifest", "inspect", "registry/agentlab:0.1.18"], stderr="missing image", returncode=1)

    try:
        upgrader.run(
            options(
                tmp_path,
                image=None,
                current_version="0.1.17",
                bump_patch=True,
                image_repository="registry/agentlab",
                skip_build=True,
                skip_push=True,
                tag=True,
                push_tag=True,
            )
        )
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected image verification failure")

    assert report.failed_step is not None
    assert report.failed_step.name == "Image verify"
    assert ["git", "push", "origin", "v0.1.18"] not in [call[0] for call in runner.calls]
    assert operator.calls == []


def test_skip_tests_skips_pytest_step(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    report = upgrader.run(options(tmp_path, skip_tests=True))

    assert [sys.executable, "-m", "pytest"] not in [call[0] for call in runner.calls]
    assert any(step.name == "Tests" and step.status == "skipped" for step in report.steps)


def test_skip_git_pull_skips_pull_step(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    report = upgrader.run(options(tmp_path, skip_git_pull=True))

    assert not any(call[0][:2] == ["git", "pull"] for call in runner.calls)
    assert [call[0] for call in runner.calls].count(["git", "status", "--porcelain"]) == 1
    assert any(step.name == "Git pull" and step.status == "skipped" for step in report.steps)


def test_default_test_command_uses_current_python_executable(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    upgrader.run(options(tmp_path, skip_tests=False))

    assert [sys.executable, "-m", "pytest"] in [call[0] for call in runner.calls]


def test_docker_binary_can_be_overridden(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    upgrader.run(options(tmp_path, skip_build=False, docker_bin="podman"))

    assert ["podman", "build", "-t", "registry/agentlab:new", "."] in [call[0] for call in runner.calls]


def test_preserve_cluster_and_local_config_together_fail(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    try:
        upgrader.run(
            options(
                tmp_path,
                apply=True,
                preserve_cluster_config=True,
                preserve_local_config=True,
            )
        )
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected preserve config conflict")

    assert runner.calls == []
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Validate options"


def test_successful_apply_flow_runs_steps_in_order(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(
        options(
            tmp_path,
            skip_git_pull=False,
            skip_tests=False,
            skip_build=False,
            skip_push=False,
            apply=True,
        )
    )

    assert [call[0] for call in runner.calls] == [
        ["git", "status", "--porcelain"],
        ["git", "pull", "--ff-only"],
        ["git", "status", "--porcelain"],
        [sys.executable, "-m", "pytest"],
        ["docker", "build", "-t", "registry/agentlab:new", "."],
        ["docker", "push", "registry/agentlab:new"],
    ]
    assert operator.calls[0] == (
        "upgrade",
        {
            "image": "registry/agentlab:new",
            "apply": True,
            "preserve_cluster_config": True,
            "preserve_local_config": False,
            "run_doctor": True,
            "show_status": True,
            "cleanup_failed": True,
        },
    )
    assert operator.calls[1][0] == "status"
    assert report.failed_step is None


def test_missing_manifest_dir_with_apply_fails_before_tests_or_docker(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    try:
        upgrader.run(
            options(
                tmp_path,
                apply=True,
                create_manifest_dir=False,
                skip_tests=False,
                skip_build=False,
                skip_push=False,
            )
        )
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected manifest preflight failure")

    assert [call[0] for call in runner.calls] == [["git", "status", "--porcelain"]]
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Kubernetes manifest preflight"
    rendered = format_release_report(report)
    assert "Kubernetes manifest dir is missing" in rendered
    assert "Docker build" not in rendered


def test_missing_manifest_dir_with_bootstrap_runs_before_tests(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    opts = options(
        tmp_path,
        apply=True,
        create_manifest_dir=False,
        bootstrap_k8s=True,
        gitlab_url="https://gitlab.example.com",
        project_id="5",
        target_repo_url="https://gitlab.example.com/group/project.git",
        ollama_url="http://ollama:11434",
        skip_tests=False,
    )
    runner.on_run(opts.bootstrap_command(), lambda: opts.resolved_manifest_dir().mkdir(parents=True, exist_ok=True))

    report = upgrader.run(opts)

    commands = [call[0] for call in runner.calls]
    assert opts.bootstrap_command() in commands
    assert [sys.executable, "-m", "pytest"] in commands
    assert commands.index(opts.bootstrap_command()) < commands.index([sys.executable, "-m", "pytest"])
    assert opts.bootstrap_command()[0] == sys.executable
    assert ["python3", "scripts/bootstrap_k8s.py"] not in commands
    assert operator.calls[0][0] == "upgrade"
    assert any(step.name == "Kubernetes manifest preflight" and step.detail == "present after bootstrap" for step in report.steps)


def test_bootstrap_failure_stops_before_tests_or_docker(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    opts = options(
        tmp_path,
        apply=True,
        create_manifest_dir=False,
        bootstrap_k8s=True,
        gitlab_url="https://gitlab.example.com",
        project_id="5",
        target_repo_url="https://gitlab.example.com/group/project.git",
        ollama_url="http://ollama:11434",
        skip_tests=False,
        skip_build=False,
        skip_push=False,
    )
    runner.respond(opts.bootstrap_command(), stderr="bootstrap failed", returncode=1)

    try:
        upgrader.run(opts)
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected bootstrap failure")

    commands = [call[0] for call in runner.calls]
    assert opts.bootstrap_command() in commands
    assert [sys.executable, "-m", "pytest"] not in commands
    assert ["docker", "build", "-t", "registry/agentlab:new", "."] not in commands
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Kubernetes bootstrap"


def test_generated_only_dirty_tree_is_allowed_by_default(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()
    runner.respond(["git", "status", "--porcelain"], stdout="?? deploy/kubernetes/generated/\n")

    report = upgrader.run(options(tmp_path))

    assert report.steps[0].detail == "generated manifests dirty allowed"


def test_git_pull_uses_ff_only_and_status_is_checked_after_pull(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    upgrader.run(options(tmp_path, skip_git_pull=False))

    assert [call[0] for call in runner.calls][:3] == [
        ["git", "status", "--porcelain"],
        ["git", "pull", "--ff-only"],
        ["git", "status", "--porcelain"],
    ]


def test_git_pull_can_disable_ff_only(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    upgrader.run(options(tmp_path, skip_git_pull=False, pull_ff_only=False))

    assert ["git", "pull"] in [call[0] for call in runner.calls]


def test_non_generated_dirty_after_pull_fails_before_tests(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()
    runner.responses[("git", "status", "--porcelain")] = [
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout="", returncode=0),
        ReleaseCommandResult(args=["git", "status", "--porcelain"], stdout=" M agentlab/release_upgrade.py\n", returncode=0),
    ]

    try:
        upgrader.run(options(tmp_path, skip_git_pull=False, skip_tests=False))
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected post-pull dirty tree failure")

    assert [sys.executable, "-m", "pytest"] not in [call[0] for call in runner.calls]
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Git status after pull"


def test_prepare_only_runs_tests_and_skips_docker_and_k8s(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, prepare_only=True, skip_tests=False, skip_build=False, skip_push=False))

    commands = [call[0] for call in runner.calls]
    assert [sys.executable, "-m", "pytest"] in commands
    assert ["docker", "build", "-t", "registry/agentlab:new", "."] not in commands
    assert ["docker", "push", "registry/agentlab:new"] not in commands
    assert operator.calls == []
    assert any(step.name == "Kubernetes upgrade" and step.detail == "--prepare-only" for step in report.steps)


def test_prepare_only_missing_manifest_dir_fails_before_tests(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    try:
        upgrader.run(options(tmp_path, prepare_only=True, create_manifest_dir=False, skip_tests=False))
    except ReleaseUpgradeError as exc:
        report = exc.report
    else:
        raise AssertionError("expected manifest preflight failure")

    assert [call[0] for call in runner.calls] == [["git", "status", "--porcelain"]]
    assert [sys.executable, "-m", "pytest"] not in [call[0] for call in runner.calls]
    assert operator.calls == []
    assert report.failed_step is not None
    assert report.failed_step.name == "Kubernetes manifest preflight"


def test_release_upgrade_propagates_doctor_warning_as_nonfatal(tmp_path: Path) -> None:
    operator = FakeK8sOperator()
    operator.doctor_status = "warning"
    upgrader, _runner, _operator = make_upgrader(operator)

    report = upgrader.run(options(tmp_path, apply=True))

    upgrade_step = next(step for step in report.steps if step.name == "Kubernetes upgrade")
    assert upgrade_step.status == "passed"
    assert "- doctor: warning" in upgrade_step.stdout
    assert report.failed_step is None


def test_release_upgrade_cli_dry_run_invocation(monkeypatch) -> None:
    monkeypatch.setattr(release_upgrade.sys, "executable", "agentlab-current-python")

    result = CliRunner().invoke(app, ["release", "upgrade", "--image", "registry/agentlab:new", "--dry-run"])

    assert result.exit_code == 0
    assert "AgentLab release upgrade" in result.output
    assert "docker build -t registry/agentlab:new ." in result.output
    assert "agentlab-current-python -m pytest" in result.output
    assert "Command: python3 -m pytest" not in result.output
