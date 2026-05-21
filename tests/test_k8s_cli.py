from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import agentlab.k8s_cli as k8s_cli
from agentlab.k8s_operator import CleanupReport, FailedResources
from agentlab.main import app


runner = CliRunner()


class FakeOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def latest_job_name(self, component: str) -> str:
        self.calls.append(("latest_job_name", component))
        return f"agentlab-scheduler-{component}"

    def job_logs(self, job_name: str, *, follow: bool = True, tail: int | None = None) -> str:
        self.calls.append(("job_logs", (job_name, follow, tail)))
        return "log output"

    def failed_resources(self) -> FailedResources:
        self.calls.append(("failed_resources", None))
        return FailedResources(jobs=["agentlab-failed-job"], pods=["agentlab-failed-pod"])

    def cleanup_failed(self, *, dry_run: bool = False) -> CleanupReport:
        self.calls.append(("cleanup_failed", dry_run))
        return CleanupReport(
            namespace="agentlab",
            deleted_jobs=["agentlab-failed-job"],
            deleted_pods=["agentlab-failed-pod"],
            dry_run=dry_run,
        )

    def upgrade(self, **kwargs):
        self.calls.append(("upgrade", kwargs))
        return type(
            "Upgrade",
            (),
            {
                "namespace": "agentlab",
                "manifest_dir": "deploy/kubernetes/generated",
                "image": kwargs["image"],
                "updated_manifests": ["configmap.yaml", "job-doctor.yaml"],
                "preserved_sections": ["auto_approve"] if kwargs.get("preserve_local_config") else [],
                "apply": kwargs.get("apply", False),
                "applied": kwargs.get("apply", False),
                "run_doctor": kwargs.get("run_doctor", False),
                "doctor_status": "not requested",
                "cleanup_failed": kwargs.get("cleanup_failed", False),
                "cleanup_report": None,
                "status_checked": False,
                "image_drift": [],
            },
        )()


class EmptyCleanupOperator(FakeOperator):
    def failed_resources(self) -> FailedResources:
        self.calls.append(("failed_resources", None))
        return FailedResources()


def test_static_completion_candidates() -> None:
    assert k8s_cli.complete_log_component() == ["latest", "watch", "plan", "action", "review-comments", "doctor"]
    assert k8s_cli.complete_log_component("re") == ["review-comments"]
    assert k8s_cli.complete_run_component() == ["watch", "plan", "action", "review-comments", "doctor", "reset-state"]
    assert k8s_cli.complete_cronjob() == ["watch", "plan", "action", "review-comments"]
    assert k8s_cli.complete_run_id() == ["latest"]
    assert "proposed.diff" in k8s_cli.complete_artifact()
    assert k8s_cli.complete_artifact("structured") == [
        "structured_proposal.json",
        "structured_proposal_report.json",
    ]


def test_k8s_command_invocation_still_works_with_completions(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "logs", "action", "--no-follow"])

    assert result.exit_code == 0
    assert "Selected Job: agentlab-scheduler-action" in result.output
    assert "log output" in result.output
    assert fake.calls == [
        ("latest_job_name", "action"),
        ("job_logs", ("agentlab-scheduler-action", False, None)),
    ]


def test_show_completion_still_works() -> None:
    result = runner.invoke(app, ["--show-completion", "bash"])

    assert result.exit_code == 0
    assert "_TYPER_COMPLETE_ARGS" in result.output or "complete" in result.output.lower()


def test_cleanup_failed_dry_run_does_not_delete(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "cleanup-failed", "--dry-run"])

    assert result.exit_code == 0
    assert "Found failed AgentLab resources in namespace agentlab" in result.output
    assert "Dry run: no resources deleted" in result.output
    assert fake.calls == [
        ("failed_resources", None),
        ("cleanup_failed", True),
    ]


def test_cleanup_failed_yes_deletes_without_prompt(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "cleanup-failed", "--yes"])

    assert result.exit_code == 0
    assert "Delete these resources?" not in result.output
    assert "job/agentlab-failed-job" in result.output
    assert fake.calls == [
        ("failed_resources", None),
        ("cleanup_failed", False),
    ]


def test_cleanup_failed_default_requires_confirmation(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "cleanup-failed"], input="n\n")

    assert result.exit_code == 0
    assert "Delete these resources?" in result.output
    assert "Cleanup cancelled." in result.output
    assert fake.calls == [("failed_resources", None)]


def test_cleanup_failed_prints_when_nothing_found(monkeypatch) -> None:
    fake = EmptyCleanupOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "cleanup-failed", "--yes"])

    assert result.exit_code == 0
    assert "No failed AgentLab resources found." in result.output
    assert fake.calls == [("failed_resources", None)]


def test_upgrade_command_invokes_operator_without_apply_by_default(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "upgrade", "--image", "registry/agentlab:new"])

    assert result.exit_code == 0
    assert "AgentLab Kubernetes upgrade plan" in result.output
    assert fake.calls == [
        (
            "upgrade",
            {
                "image": "registry/agentlab:new",
                "apply": False,
                "preserve_cluster_config": False,
                "preserve_local_config": False,
                "run_doctor": False,
                "show_status": False,
                "cleanup_failed": False,
            },
        )
    ]


def test_upgrade_apply_with_yes_invokes_apply_options(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(
        app,
        [
            "k8s",
            "upgrade",
            "--image",
            "registry/agentlab:new",
            "--apply",
            "--yes",
            "--preserve-local-config",
            "--run-doctor",
            "--cleanup-failed",
        ],
    )

    assert result.exit_code == 0
    assert "Continue?" not in result.output
    assert fake.calls[0][1]["apply"] is True
    assert fake.calls[0][1]["preserve_local_config"] is True
    assert fake.calls[0][1]["run_doctor"] is True
    assert fake.calls[0][1]["cleanup_failed"] is True
