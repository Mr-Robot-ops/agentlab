from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import agentlab.k8s_cli as k8s_cli
from agentlab.k8s_operator import (
    CleanupReport,
    ConfigSetReport,
    ConfigValueReport,
    FailedResources,
    HealthReport,
    MergeRequestListReport,
)
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

    def run_component(self, component: str, *, follow: bool = True, task_id: str | None = None) -> str:
        self.calls.append(("run_component", (component, follow, task_id)))
        return f"manifest-{component}"

    def config_get(self, path: str) -> ConfigValueReport:
        self.calls.append(("config_get", path))
        return ConfigValueReport(path=path, value=True)

    def config_set(self, path: str, value: str) -> ConfigSetReport:
        self.calls.append(("config_set", (path, value)))
        return ConfigSetReport(path=path, before=False, after=value == "true", changed=True)

    def mrs(
        self,
        *,
        state: str = "opened",
        label: str = "agent/generated",
        secret_name: str = "agentlab-secrets",
    ) -> MergeRequestListReport:
        self.calls.append(("mrs", (state, label, secret_name)))
        return MergeRequestListReport(
            namespace="agentlab",
            state=state,
            label=label,
            merge_requests=[
                {
                    "iid": 18,
                    "title": "Add smoke baseline",
                    "state": state,
                    "source_branch": "agent/tests-02-smoke-baseline",
                    "web_url": "https://gitlab.example.com/group/project/-/merge_requests/18",
                    "labels": [label],
                    "updated_at": "2026-05-22T12:00:00Z",
                }
            ],
        )

    def health(
        self,
        *,
        manifest_dir: Path | None = None,
        pvc: str = "agentlab-runs",
        shell_pod: str = "artifact-shell",
    ) -> HealthReport:
        self.calls.append(("health", (manifest_dir, pvc, shell_pod)))
        return HealthReport(
            namespace="agentlab",
            status="warning",
            images={
                "configmap_image": "registry/agentlab:new",
                "generated_configmap_image": "registry/agentlab:new",
                "cronjobs": [],
                "drift": [],
            },
            failed_resources={"jobs": [], "pods": []},
            open_agent_mrs=[
                {
                    "iid": 18,
                    "title": "Add smoke baseline",
                    "source_branch": "agent/tests-02-smoke-baseline",
                    "web_url": "https://gitlab.example.com/group/project/-/merge_requests/18",
                    "labels": ["agent/generated"],
                    "updated_at": "2026-05-22T12:00:00Z",
                }
            ],
            gitlab={
                "url": "https://gitlab.example.com",
                "project_id": 1,
                "open_agent_mrs_count": 1,
                "status": "ok",
                "warning": None,
            },
            scheduler={
                "state_path": "/var/lib/agentlab/runs/scheduler/state.json",
                "state_exists": True,
                "state_age_seconds": 60,
                "last_watch_run": "2026-05-22T09:00:00Z",
                "last_plan_run": "2026-05-22T10:00:00Z",
                "last_action_run": "2026-05-22T11:00:00Z",
                "last_review_run": "2026-05-22T12:00:00Z",
                "action_enabled": True,
                "review_comments": {
                    "enabled": True,
                    "allowed_authors": ["alice"],
                    "require_author_role": ["owner"],
                },
            },
            models={"base_url": "http://ollama.local:11434", "default": "qwen-health", "models": {"default": "qwen-health"}},
            doctor={"status": "warning", "job": "agentlab-doctor"},
            warnings=["doctor status: warning"],
        )

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
    assert "functional_test_report.json" in k8s_cli.complete_artifact()
    assert "raw_patch.diff" in k8s_cli.complete_artifact()
    assert "schedule.action.enabled" in k8s_cli.complete_config_path("schedule.action")


def test_k8s_help_alias_lists_key_commands() -> None:
    result = runner.invoke(app, ["k8s", "help"])

    assert result.exit_code == 0
    for command in ("status", "run", "artifact", "upgrade", "config", "health", "mrs", "tui"):
        assert command in result.output


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


def test_k8s_run_action_passes_task_id(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)
    monkeypatch.setattr(k8s_cli, "manifest_for_component", lambda component, manifest_dir: Path("job-scheduler-action.yaml"))
    monkeypatch.setattr(k8s_cli, "run_job_name_for_component", lambda component: "agentlab-scheduler-action")

    result = runner.invoke(
        app,
        ["k8s", "run", "action", "--task-id", "tests-02-smoke-baseline", "--no-follow"],
    )

    assert result.exit_code == 0
    assert fake.calls == [
        ("run_component", ("action", False, "tests-02-smoke-baseline")),
    ]


def test_k8s_config_get_invokes_operator(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "config", "get", "schedule.action.enabled"])

    assert result.exit_code == 0
    assert "schedule.action.enabled: true" in result.output
    assert fake.calls == [("config_get", "schedule.action.enabled")]


def test_k8s_config_set_invokes_operator_and_prints_before_after(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "config", "set", "schedule.action.enabled", "true"])

    assert result.exit_code == 0
    assert "Before: false" in result.output
    assert "After: true" in result.output
    assert fake.calls == [("config_set", ("schedule.action.enabled", "true"))]


def test_k8s_mrs_invokes_operator_and_prints_table(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "mrs", "--state", "opened", "--label", "agent/generated"])

    assert result.exit_code == 0
    assert (
        "!18 | Add smoke baseline | opened | agent/tests-02-smoke-baseline | "
        "https://gitlab.example.com/group/project/-/merge_requests/18 | agent/generated"
    ) in result.output
    assert fake.calls == [("mrs", ("opened", "agent/generated", "agentlab-secrets"))]


def test_k8s_mrs_supports_json(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "mrs", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["state"] == "opened"
    assert payload["label"] == "agent/generated"
    assert payload["merge_requests"][0]["iid"] == 18
    assert "super-secret" not in result.output
    assert fake.calls == [("mrs", ("opened", "agent/generated", "agentlab-secrets"))]


def test_k8s_health_invokes_operator_and_prints_summary(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "health", "--manifest-dir", "deploy/kubernetes/generated"])

    assert result.exit_code == 0
    assert "AgentLab health: warning" in result.output
    assert "Open Agent MRs: 1" in result.output
    assert "action: enabled" in result.output
    assert fake.calls == [("health", (Path("deploy/kubernetes/generated"), "agentlab-runs", "artifact-shell"))]


def test_k8s_health_supports_json(monkeypatch) -> None:
    fake = FakeOperator()
    monkeypatch.setattr(k8s_cli, "_operator", lambda namespace, manifest_dir=Path("deploy/kubernetes/generated"): fake)

    result = runner.invoke(app, ["k8s", "health", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "warning"
    assert payload["open_agent_mrs"][0]["iid"] == 18
    assert payload["scheduler"]["action_enabled"] is True
    assert payload["doctor"]["status"] == "warning"
    assert "super-secret" not in result.output
    assert fake.calls == [("health", (Path("deploy/kubernetes/generated"), "agentlab-runs", "artifact-shell"))]


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
