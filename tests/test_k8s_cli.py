from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import agentlab.k8s_cli as k8s_cli
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
