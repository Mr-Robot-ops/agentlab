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
    ReleaseUpgrader,
    format_release_report,
)


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.responses: dict[tuple[str, ...], ReleaseCommandResult] = {}

    def respond(self, args: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.responses[tuple(args)] = ReleaseCommandResult(args=args, stdout=stdout, stderr=stderr, returncode=returncode)

    def run(self, args: list[str], *, cwd: Path) -> ReleaseCommandResult:
        self.calls.append((args, cwd))
        return self.responses.get(tuple(args), ReleaseCommandResult(args=args))


class FakeK8sOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.upgrade_image_drift: list[str] = []
        self.status_image_drift = False
        self.doctor_status = "completed"

    def upgrade(self, **kwargs):
        self.calls.append(("upgrade", kwargs))
        return type(
            "Upgrade",
            (),
            {
                "namespace": "agentlab",
                "manifest_dir": "deploy/kubernetes/generated",
                "image": kwargs["image"],
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
        return ClusterStatus(namespace="agentlab", configmap_image="registry/agentlab:new")


def make_upgrader(fake_operator: FakeK8sOperator | None = None) -> tuple[ReleaseUpgrader, FakeCommandRunner, FakeK8sOperator]:
    runner = FakeCommandRunner()
    operator = fake_operator or FakeK8sOperator()
    upgrader = ReleaseUpgrader(command_runner=runner, operator_factory=lambda namespace, manifest_dir: operator)
    return upgrader, runner, operator


def options(tmp_path: Path, **updates) -> ReleaseUpgradeOptions:
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


def test_dry_run_prints_planned_commands_and_executes_nothing(tmp_path: Path) -> None:
    upgrader, runner, operator = make_upgrader()

    report = upgrader.run(options(tmp_path, dry_run=True, skip_git_pull=False, skip_tests=False, skip_build=False, skip_push=False))
    rendered = format_release_report(report)

    assert runner.calls == []
    assert operator.calls == []
    assert "git pull" in rendered
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


def test_skip_tests_skips_pytest_step(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    report = upgrader.run(options(tmp_path, skip_tests=True))

    assert [sys.executable, "-m", "pytest"] not in [call[0] for call in runner.calls]
    assert any(step.name == "Tests" and step.status == "skipped" for step in report.steps)


def test_skip_git_pull_skips_pull_step(tmp_path: Path) -> None:
    upgrader, runner, _operator = make_upgrader()

    report = upgrader.run(options(tmp_path, skip_git_pull=True))

    assert ["git", "pull"] not in [call[0] for call in runner.calls]
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
        ["git", "pull"],
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
