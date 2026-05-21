from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlab.k8s_operator import (
    ArtifactNotFoundError,
    K8sOperator,
    K8sOperatorError,
    K8sTUI,
    KubectlResult,
    TuiUnavailableError,
    artifact_path,
    cronjob_for_component,
    detect_manifest_image_drift,
    job_prefix_for_component,
    kubectl_args,
    manifest_for_component,
    run_tui,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.stream_calls: list[list[str]] = []
        self.interactive_calls: list[list[str]] = []
        self.responses: dict[tuple[str, ...], KubectlResult] = {}

    def respond(self, args: list[str], stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.responses[tuple(args)] = KubectlResult(args=args, stdout=stdout, stderr=stderr, returncode=returncode)

    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> KubectlResult:
        self.calls.append((args, input_text))
        result = self.responses.get(tuple(args), KubectlResult(args=args, stdout="{}", returncode=0))
        if check and result.returncode != 0:
            raise K8sOperatorError(result.stderr or result.stdout or "kubectl failed")
        return result

    def stream(self, args: list[str]) -> int:
        self.stream_calls.append(args)
        return 0

    def interactive(self, args: list[str]) -> int:
        self.interactive_calls.append(args)
        return 0


class FakeTTY:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class FakeOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def status(self):
        self.calls.append(("status", None))
        return "status"

    def runs(self):
        self.calls.append(("runs", None))
        return []

    def logs(self, component: str, *, follow: bool = True, tail: int | None = None):
        self.calls.append(("logs", (component, follow, tail)))
        return "job", "logs"

    def run_component(self, component: str, *, follow: bool = True):
        self.calls.append(("run_component", (component, follow)))
        return f"manifest-{component}"

    def artifact(self, run_id: str, artifact: str):
        self.calls.append(("artifact", (run_id, artifact)))
        return type("Artifact", (), {"path": "/path", "content": "content"})()

    def set_cronjob_suspend(self, component: str, suspend: bool):
        self.calls.append(("set_cronjob_suspend", (component, suspend)))
        return f"{component}:{suspend}"

    def shell(self):
        self.calls.append(("shell", None))
        return 0


def test_component_mappings() -> None:
    assert job_prefix_for_component("review-comments") == "agentlab-scheduler-review-comments"
    assert manifest_for_component("reset-state", Path("generated")) == Path("generated/job-scheduler-reset-state.yaml")
    assert cronjob_for_component("action") == "agentlab-scheduler-action"


def test_kubectl_args_construction() -> None:
    assert kubectl_args("agentlab", ["get", "jobs"]) == ["-n", "agentlab", "get", "jobs"]


def test_run_component_uses_generated_manifest_and_fixed_job_name(tmp_path: Path) -> None:
    manifest = tmp_path / "job-scheduler-review-comments.yaml"
    manifest.write_text("kind: Job\n", encoding="utf-8")
    runner = FakeRunner()

    used = K8sOperator(manifest_dir=tmp_path, runner=runner).run_component("review-comments", follow=False)

    assert used == str(manifest)
    assert runner.calls[0][0] == [
        "-n",
        "agentlab",
        "delete",
        "job",
        "agentlab-scheduler-review-comments",
        "--ignore-not-found=true",
    ]
    assert runner.calls[1][0] == ["-n", "agentlab", "apply", "-f", str(manifest)]


def test_run_component_fails_clearly_when_manifest_missing(tmp_path: Path) -> None:
    with pytest.raises(K8sOperatorError, match="Re-run Kubernetes bootstrap"):
        K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).run_component("reset-state", follow=False)


def test_latest_job_selection_from_mocked_kubectl_output() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {
                            "name": "agentlab-scheduler-action-1",
                            "creationTimestamp": "2026-05-01T00:00:00Z",
                        },
                        "status": {"succeeded": 1},
                    },
                    {
                        "metadata": {
                            "name": "agentlab-scheduler-action-2",
                            "creationTimestamp": "2026-05-02T00:00:00Z",
                        },
                        "status": {"succeeded": 1},
                    },
                ]
            }
        ),
    )

    assert K8sOperator(runner=runner).latest_job_name("action") == "agentlab-scheduler-action-2"


def test_artifact_path_construction() -> None:
    assert (
        artifact_path("b9c483f7c10f4a5b807e8d626b664574", "gate_decision.json")
        == "/var/lib/agentlab/runs/b9c483f7c10f4a5b807e8d626b664574/artifacts/gate_decision.json"
    )


def test_missing_artifact_error_message_lists_available_artifacts() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "pod", "artifact-shell", "-o", "json"], json.dumps({"status": {}}))
    runner.respond(["-n", "agentlab", "wait", "--for=condition=Ready", "pod/artifact-shell", "--timeout=60s"])
    runner.respond(
        [
            "-n",
            "agentlab",
            "exec",
            "artifact-shell",
            "--",
            "sh",
            "-c",
            "test -f '/var/lib/agentlab/runs/run1/artifacts/missing.json'",
        ],
        returncode=1,
    )
    runner.respond(
        [
            "-n",
            "agentlab",
            "exec",
            "artifact-shell",
            "--",
            "sh",
            "-c",
            "ls -1 '/var/lib/agentlab/runs/run1/artifacts' 2>/dev/null || true",
        ],
        "gate_decision.json\nproposed.diff\n",
    )

    with pytest.raises(ArtifactNotFoundError) as exc:
        K8sOperator(runner=runner).artifact("run1", "missing.json")

    assert "gate_decision.json" in str(exc.value)
    assert exc.value.available_artifacts == ["gate_decision.json", "proposed.diff"]


def test_image_drift_detection_from_mocked_cluster_output() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps({"metadata": {"annotations": {"agentlab.io/image": "registry/agentlab:new"}}}),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "agentlab-scheduler-action"},
                        "spec": {
                            "schedule": "*/5 * * * *",
                            "jobTemplate": {
                                "spec": {
                                    "template": {
                                        "spec": {"containers": [{"image": "registry/agentlab:old"}]}
                                    }
                                }
                            },
                        },
                        "status": {},
                    }
                ]
            }
        ),
    )
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.cronjobs[0].image_drift is True


def test_manifest_image_drift_detection(tmp_path: Path) -> None:
    (tmp_path / "configmap.yaml").write_text(
        """
kind: ConfigMap
metadata:
  annotations:
    agentlab.io/image: registry/agentlab:new
""",
        encoding="utf-8",
    )
    (tmp_path / "job-scheduler-action.yaml").write_text(
        """
kind: Job
spec:
  template:
    spec:
      containers:
        - image: registry/agentlab:old
""",
        encoding="utf-8",
    )

    image, drifts = detect_manifest_image_drift(tmp_path)

    assert image == "registry/agentlab:new"
    assert drifts[0].path == "job-scheduler-action.yaml"


def test_tui_action_mapping_calls_same_operator_helpers() -> None:
    operator = FakeOperator()
    answers = iter(["3"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
        confirm_func=lambda _message: True,
    )

    tui.run_once("4")

    assert operator.calls == [("run_component", ("action", True))]


def test_tui_non_interactive_fallback() -> None:
    with pytest.raises(TuiUnavailableError, match="Interactive TUI requires a TTY"):
        run_tui(K8sOperator(runner=FakeRunner()), stdin=FakeTTY(False), stdout=FakeTTY(True))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("choice", "answers", "expected_message"),
    [
        ("2", [], "Create artifact-shell pod if missing to list runs?"),
        ("4", ["3"], "This may create or update a Merge Request. Continue?"),
        ("4", ["6"], "This clears scheduler state. Continue?"),
        ("5", [], "Create artifact-shell pod if missing to read artifacts?"),
        ("6", [], "This clears scheduler state. Continue?"),
        ("7", ["4"], "Suspend CronJob agentlab-scheduler-review-comments?"),
        ("8", ["4"], "Resume CronJob agentlab-scheduler-review-comments?"),
        ("9", [], "Create artifact-shell pod if missing and open shell?"),
    ],
)
def test_tui_requires_confirmation_for_mutating_actions(
    choice: str,
    answers: list[str],
    expected_message: str,
) -> None:
    operator = FakeOperator()
    messages: list[str] = []
    answer_iter = iter(answers)
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answer_iter),
        output_func=lambda _text: None,
        confirm_func=lambda message: messages.append(message) or False,
    )

    tui.run_once(choice)

    assert messages == [expected_message]
    assert operator.calls == []
