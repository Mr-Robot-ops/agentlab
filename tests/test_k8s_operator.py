from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentlab.k8s_operator import (
    ArtifactNotFoundError,
    DEPRECATED_K8S_IMAGE_ANNOTATION,
    DEPRECATED_K8S_IMAGE_ANNOTATION_WARNING,
    FailedResources,
    K8S_IMAGE_ANNOTATION,
    K8S_VERSION_ANNOTATION,
    K8sOperator,
    K8sOperatorError,
    K8sTUI,
    KubectlResult,
    FallbackTUIAdapter,
    QuestionaryTUIAdapter,
    QUESTIONARY_TUI_INSTALL_HINT,
    QUESTIONARY_TUI_STYLE_RULES,
    TuiChoice,
    TuiUnavailableError,
    artifact_path,
    cronjob_for_component,
    detect_manifest_image_drift,
    format_health,
    format_mrs,
    format_status,
    format_upgrade_report,
    job_prefix_for_component,
    kubectl_args,
    manifest_for_component,
    build_questionary_tui_style,
    create_tui_adapter,
    resolve_tui_choice,
    run_tui,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.stream_calls: list[list[str]] = []
        self.interactive_calls: list[list[str]] = []
        self.responses: dict[tuple[str, ...], KubectlResult | list[KubectlResult]] = {}

    def respond(self, args: list[str], stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.responses[tuple(args)] = KubectlResult(args=args, stdout=stdout, stderr=stderr, returncode=returncode)

    def respond_many(self, args: list[str], responses: list[KubectlResult]) -> None:
        self.responses[tuple(args)] = responses

    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> KubectlResult:
        self.calls.append((args, input_text))
        configured = self.responses.get(tuple(args), KubectlResult(args=args, stdout="{}", returncode=0))
        if isinstance(configured, list):
            result = configured.pop(0) if configured else KubectlResult(args=args, stdout="{}", returncode=0)
        else:
            result = configured
        if check and result.returncode != 0:
            raise K8sOperatorError(result.stderr or result.stdout or "kubectl failed")
        return result

    def stream(self, args: list[str]) -> KubectlResult:
        self.stream_calls.append(args)
        configured = self.responses.get(tuple(args), KubectlResult(args=args, returncode=0))
        if isinstance(configured, list):
            result = configured.pop(0) if configured else KubectlResult(args=args, returncode=0)
        else:
            result = configured
        return result

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
        self.namespace = "agentlab"
        self.calls: list[tuple[str, object]] = []

    def status(self):
        self.calls.append(("status", None))
        return "status"

    def runs(self, *, limit: int = 20):
        self.calls.append(("runs", None))
        return [
            SimpleNamespace(run_id="e474a44a82dc4bf8b6b8ce2732194ffc"),
            SimpleNamespace(run_id="8ad7b96953d944f4afb0e9a8648df908"),
        ]

    def logs(self, component: str, *, follow: bool = True, tail: int | None = None):
        self.calls.append(("logs", (component, follow, tail)))
        return "job", "logs"

    def latest_job_name(self, component: str) -> str:
        self.calls.append(("latest_job_name", component))
        return f"agentlab-{component}"

    def job_logs(self, job_name: str, *, follow: bool = True, tail: int | None = None) -> str:
        self.calls.append(("job_logs", (job_name, follow, tail)))
        return f"{job_name} logs"

    def run_component(self, component: str, *, follow: bool = True):
        self.calls.append(("run_component", (component, follow)))
        return f"manifest-{component}"

    def artifact(self, run_id: str, artifact: str):
        self.calls.append(("artifact", (run_id, artifact)))
        return type("Artifact", (), {"path": "/path", "content": "content"})()

    def ensure_artifact_shell(self) -> None:
        self.calls.append(("ensure_artifact_shell", None))

    def latest_run_id(self) -> str:
        self.calls.append(("latest_run_id", None))
        return "e474a44a82dc4bf8b6b8ce2732194ffc"

    def available_artifacts(self, run_id: str):
        self.calls.append(("available_artifacts", run_id))
        return ["manifest.json", "gate_decision.json", "raw_patch.diff"]

    def set_cronjob_suspend(self, component: str, suspend: bool):
        self.calls.append(("set_cronjob_suspend", (component, suspend)))
        return f"{component}:{suspend}"

    def shell(self):
        self.calls.append(("shell", None))
        return 0

    def failed_resources(self):
        self.calls.append(("failed_resources", None))
        return FailedResources(jobs=["agentlab-failed-job"], pods=["agentlab-failed-pod"])

    def cleanup_failed(self, *, dry_run: bool = False):
        self.calls.append(("cleanup_failed", dry_run))
        return type(
            "Cleanup",
            (),
            {
                "namespace": "agentlab",
                "deleted_jobs": ["agentlab-failed-job"],
                "deleted_pods": ["agentlab-failed-pod"],
                "skipped_resources": [],
                "dry_run": dry_run,
            },
        )()

    def upgrade(self, **kwargs):
        self.calls.append(("upgrade", kwargs))
        return type(
            "Upgrade",
            (),
            {
                "namespace": "agentlab",
                "manifest_dir": "generated",
                "image": kwargs["image"],
                "updated_manifests": ["configmap.yaml"],
                "preserved_sections": [],
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


def job_item(name: str, *, failed: int = 0, active: int = 0, succeeded: int = 0) -> dict[str, object]:
    return {
        "metadata": {"name": name},
        "status": {"failed": failed, "active": active, "succeeded": succeeded},
    }


def pod_item(name: str, *, phase: str, reason: str | None = None) -> dict[str, object]:
    container_status = {}
    if reason is not None:
        container_status = {"state": {"terminated": {"reason": reason}}}
    return {
        "metadata": {"name": name},
        "status": {
            "phase": phase,
            "containerStatuses": [container_status] if container_status else [],
        },
    }


def write_upgrade_manifests(
    path: Path,
    *,
    image: str = "registry/agentlab:old",
    include_review_comments_cronjob: bool = True,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "configmap.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ConfigMap",
                "metadata": {
                    "name": "agentlab-config",
                    "annotations": {K8S_IMAGE_ANNOTATION: image},
                },
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {
                            "project_id": "group/project",
                            "target_repo_url": "https://gitlab.local/group/project.git",
                            "auto_merge_enabled": False,
                            "direct_main_push_enabled": False,
                            "schedule": {"enabled": False, "review_comments": {"enabled": False}},
                            "required_test_commands": [],
                        },
                        sort_keys=False,
                    )
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for name in (
        "job-doctor.yaml",
        "job-scheduler-watch.yaml",
        "job-scheduler-plan.yaml",
        "job-scheduler-action.yaml",
        "job-scheduler-review-comments.yaml",
        "job-scheduler-reset-state.yaml",
    ):
        (path / name).write_text(
            yaml.safe_dump(
                {
                    "kind": "Job",
                    "spec": {"template": {"spec": {"containers": [{"name": "agentlab", "image": image}]}}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    cronjobs = ["cronjob-scheduler-watch.yaml"]
    if include_review_comments_cronjob:
        cronjobs.append("cronjob-scheduler-review-comments.yaml")
    for name in cronjobs:
        (path / name).write_text(
            yaml.safe_dump(
                {
                    "kind": "CronJob",
                    "spec": {
                        "jobTemplate": {
                            "spec": {
                                "template": {
                                    "spec": {"containers": [{"name": "agentlab", "image": image}]}
                                }
                            }
                        }
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    (path / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "Kustomization",
                "resources": ["configmap.yaml"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def manifest_image(path: Path) -> str:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if document["kind"] == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
    return document["spec"]["template"]["spec"]["containers"][0]["image"]


def configmap_config(path: Path) -> dict[str, object]:
    configmap = yaml.safe_load((path / "configmap.yaml").read_text(encoding="utf-8"))
    return yaml.safe_load(configmap["data"]["config.yaml"])


def cronjob_names_json(*, configmap_image: str, cronjob_images: dict[str, str]) -> tuple[str, str]:
    configmap = json.dumps({"metadata": {"annotations": {K8S_IMAGE_ANNOTATION: configmap_image}}})
    cronjobs = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": name},
                    "spec": {
                        "schedule": "*/5 * * * *",
                        "jobTemplate": {
                            "spec": {
                                "template": {
                                    "spec": {"containers": [{"image": image}]}
                                }
                            }
                        },
                    },
                    "status": {},
                }
                for name, image in cronjob_images.items()
            ]
        }
    )
    return configmap, cronjobs


def configmap_with_app_config(extra_config: dict[str, object] | None = None) -> str:
    config: dict[str, object] = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": "/workspace/repo",
        "workspace_root": "/var/lib/agentlab/runs",
    }
    if extra_config:
        config.update(extra_config)
    return json.dumps(
        {
            "metadata": {"annotations": {K8S_IMAGE_ANNOTATION: "registry/agentlab:new"}},
            "data": {"config.yaml": yaml.safe_dump(config, sort_keys=False)},
        }
    )


class FakeGitLabForStatus:
    def __init__(self, _config) -> None:
        pass

    def list_open_agent_mrs(self) -> list[object]:
        return [
            SimpleNamespace(
                id=18,
                mr_id=18,
                iid=18,
                title="Add smoke baseline",
                source_branch="agent/tests-02-smoke-baseline",
                target_branch="main",
                web_url="https://gitlab.example.com/group/project/-/merge_requests/18",
                labels=["agent/generated"],
                updated_at="2026-05-22T12:00:00Z",
            )
        ]


class FakeGitLabForMrs:
    seen_token: str | None = None
    seen_project_id: object = None
    seen_args: tuple[str, str] | None = None

    def __init__(self, config, *, token: str | None = None) -> None:
        type(self).seen_token = token
        type(self).seen_project_id = config.project_id

    def list_agent_merge_requests(self, *, state: str, label: str) -> list[object]:
        type(self).seen_args = (state, label)
        return [
            SimpleNamespace(
                iid=18,
                title="Add smoke baseline",
                state=state,
                source_branch="agent/tests-02-smoke-baseline",
                target_branch="main",
                web_url="https://gitlab.example.com/group/project/-/merge_requests/18",
                labels=[label],
                updated_at="2026-05-22T12:00:00Z",
            ),
            SimpleNamespace(
                iid=19,
                title="Manual branch",
                state=state,
                source_branch="feature/manual",
                target_branch="main",
                web_url="https://gitlab.example.com/group/project/-/merge_requests/19",
                labels=[label],
                updated_at="2026-05-22T12:01:00Z",
            ),
        ]


def gitlab_secret(token: str = "super-secret") -> str:
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    return json.dumps({"data": {"GITLAB_TOKEN": encoded}})


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


def test_run_component_action_task_id_patches_generated_job_command(tmp_path: Path) -> None:
    manifest = tmp_path / "job-scheduler-action.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "kind": "Job",
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "agentlab",
                                    "args": ["scheduler-action", "--config", "/etc/agentlab/config.yaml"],
                                }
                            ]
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runner = FakeRunner()

    used = K8sOperator(manifest_dir=tmp_path, runner=runner).run_component(
        "action",
        follow=False,
        task_id="tests-02-smoke-baseline",
    )

    assert used == str(manifest)
    assert runner.calls[1][0] == ["-n", "agentlab", "apply", "-f", "-"]
    applied = yaml.safe_load(runner.calls[1][1] or "")
    args = applied["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == [
        "scheduler-action",
        "--config",
        "/etc/agentlab/config.yaml",
        "--task-id",
        "tests-02-smoke-baseline",
    ]


def test_run_component_task_id_is_action_only(tmp_path: Path) -> None:
    manifest = tmp_path / "job-scheduler-plan.yaml"
    manifest.write_text("kind: Job\n", encoding="utf-8")

    with pytest.raises(K8sOperatorError, match="only supported for the action component"):
        K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).run_component(
            "plan",
            follow=False,
            task_id="tests-02-smoke-baseline",
        )


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
        json.dumps({"metadata": {"annotations": {K8S_IMAGE_ANNOTATION: "registry/agentlab:new"}}}),
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


def test_status_reads_deprecated_image_annotation_with_warning() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps({"metadata": {"annotations": {DEPRECATED_K8S_IMAGE_ANNOTATION: "registry/agentlab:new"}}}),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps({"items": []}),
    )
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.configmap_image == "registry/agentlab:new"
    assert status.image_annotation_warning == DEPRECATED_K8S_IMAGE_ANNOTATION_WARNING
    assert DEPRECATED_K8S_IMAGE_ANNOTATION_WARNING in format_status(status)


def test_status_prefers_new_image_annotation_when_both_exist() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "metadata": {
                    "annotations": {
                        DEPRECATED_K8S_IMAGE_ANNOTATION: "registry/agentlab:old",
                        K8S_IMAGE_ANNOTATION: "registry/agentlab:new",
                    }
                }
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps({"items": []}),
    )
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.configmap_image == "registry/agentlab:new"


def test_status_ignores_unowned_github_pages_image_annotation() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps({"metadata": {"annotations": {"agentlab.github.io/agentlab-image": "registry/agentlab:wrong"}}}),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps({"items": []}),
    )
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.configmap_image is None
    assert status.image_annotation_warning is None


def test_status_reads_release_version_annotation() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps({"metadata": {"annotations": {K8S_VERSION_ANNOTATION: "v0.1.18"}}}),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps({"items": []}),
    )
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.configmap_version == "v0.1.18"
    assert "ConfigMap version: v0.1.18" in format_status(status)


def test_manifest_image_drift_detection(tmp_path: Path) -> None:
    (tmp_path / "configmap.yaml").write_text(
        """
kind: ConfigMap
metadata:
  annotations:
    mr-robot-ops.github.io/agentlab-image: registry/agentlab:new
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


def test_manifest_image_drift_reads_deprecated_annotation_fallback(tmp_path: Path) -> None:
    (tmp_path / "configmap.yaml").write_text(
        f"""
kind: ConfigMap
metadata:
  annotations:
    {DEPRECATED_K8S_IMAGE_ANNOTATION}: registry/agentlab:new
""",
        encoding="utf-8",
    )

    image, drifts = detect_manifest_image_drift(tmp_path)

    assert image == "registry/agentlab:new"
    assert drifts == []


def test_upgrade_updates_configmap_annotation_and_all_workload_images(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(image="registry/agentlab:new")

    configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    assert configmap["metadata"]["annotations"][K8S_IMAGE_ANNOTATION] == "registry/agentlab:new"
    assert DEPRECATED_K8S_IMAGE_ANNOTATION not in configmap["metadata"]["annotations"]
    for path in [*tmp_path.glob("job-*.yaml"), *tmp_path.glob("cronjob-*.yaml")]:
        assert manifest_image(path) == "registry/agentlab:new"
    assert "job-scheduler-reset-state.yaml" in report.updated_manifests
    assert "cronjob-scheduler-review-comments.yaml" in report.updated_manifests
    assert report.image_drift == []


def test_upgrade_adds_missing_job_resource_safeguards(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(image="registry/agentlab:old")

    job = yaml.safe_load((tmp_path / "job-scheduler-action.yaml").read_text(encoding="utf-8"))
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 3600
    assert job["spec"]["ttlSecondsAfterFinished"] == 86400
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }
    job_env = {item["name"]: item for item in container["env"]}
    assert job_env["PATH"]["value"] == "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin"
    cronjob = yaml.safe_load((tmp_path / "cronjob-scheduler-watch.yaml").read_text(encoding="utf-8"))
    cronjob_container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"
    assert cronjob["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    assert cronjob["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 3600
    assert cronjob_container["resources"] == container["resources"]
    cronjob_env = {item["name"]: item for item in cronjob_container["env"]}
    assert cronjob_env["PATH"]["value"] == "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin"
    assert "job-scheduler-action.yaml" in report.updated_manifests
    assert "cronjob-scheduler-watch.yaml" in report.updated_manifests


def test_upgrade_migrates_deprecated_configmap_image_annotation(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    annotations = configmap["metadata"]["annotations"]
    annotations.pop(K8S_IMAGE_ANNOTATION)
    annotations[DEPRECATED_K8S_IMAGE_ANNOTATION] = "registry/agentlab:old"
    (tmp_path / "configmap.yaml").write_text(yaml.safe_dump(configmap, sort_keys=False), encoding="utf-8")

    K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(image="registry/agentlab:new")

    migrated = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    assert migrated["metadata"]["annotations"][K8S_IMAGE_ANNOTATION] == "registry/agentlab:new"
    assert DEPRECATED_K8S_IMAGE_ANNOTATION not in migrated["metadata"]["annotations"]


def test_upgrade_writes_release_version_annotation(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(
        image="registry/agentlab:new",
        version="v0.1.18",
    )

    configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    assert configmap["metadata"]["annotations"][K8S_VERSION_ANNOTATION] == "v0.1.18"
    assert report.version == "v0.1.18"


def test_upgrade_preserves_selected_local_config_sections(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    config["auto_approve"] = {"enabled": True, "allowed_paths": ["README.md"]}
    config["schedule"] = {
        "enabled": True,
        "review_comments": {"enabled": True, "cron": "*/1 * * * *"},
        "limits": {"max_daily_mrs": 3},
        "behavior": {"skip_if_open_agent_mr_exists": False},
    }
    config["required_test_commands"] = ["python -m pytest"]
    config["auto_merge_enabled"] = True
    configmap["metadata"]["annotations"][K8S_IMAGE_ANNOTATION] = "registry/agentlab:old"
    configmap["data"]["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
    (tmp_path / "configmap.yaml").write_text(yaml.safe_dump(configmap, sort_keys=False), encoding="utf-8")

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(
        image="registry/agentlab:new",
        preserve_local_config=True,
    )

    new_configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    merged = yaml.safe_load(new_configmap["data"]["config.yaml"])
    assert new_configmap["metadata"]["annotations"][K8S_IMAGE_ANNOTATION] == "registry/agentlab:new"
    assert DEPRECATED_K8S_IMAGE_ANNOTATION not in new_configmap["metadata"]["annotations"]
    assert merged["auto_approve"] == {"enabled": True, "allowed_paths": ["README.md"]}
    assert merged["schedule"]["review_comments"]["enabled"] is True
    assert merged["schedule"]["limits"]["max_daily_mrs"] == 3
    assert merged["schedule"]["behavior"]["skip_if_open_agent_mr_exists"] is False
    assert merged["required_test_commands"] == ["python -m pytest"]
    assert merged["auto_merge_enabled"] is False
    assert merged["direct_main_push_enabled"] is False
    assert "auto_approve" in report.preserved_sections
    assert "schedule.review_comments" in report.preserved_sections
    assert "schedule.limits" in report.preserved_sections
    assert "schedule.behavior" in report.preserved_sections


def test_upgrade_preserves_cluster_config_and_rejects_conflicting_preserve_sources(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {
                            "auto_approve": {"enabled": True},
                            "schedule": {"review_comments": {"enabled": True}},
                        },
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
        image="registry/agentlab:new",
        preserve_cluster_config=True,
    )

    assert configmap_config(tmp_path)["auto_approve"]["enabled"] is True
    with pytest.raises(K8sOperatorError, match="Choose either"):
        K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
            image="registry/agentlab:new",
            preserve_cluster_config=True,
            preserve_local_config=True,
        )


def test_config_get_reads_allowed_configmap_value() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {"schedule": {"action": {"enabled": False}}},
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    report = K8sOperator(runner=runner).config_get("schedule.action.enabled")

    assert report.path == "schedule.action.enabled"
    assert report.value is False
    assert report.exists is True
    assert runner.calls == [
        (["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], None)
    ]


def test_config_set_patches_only_config_yaml_and_preserves_annotations() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "metadata": {
                    "annotations": {
                        K8S_IMAGE_ANNOTATION: "registry/agentlab:current",
                        "operator.note": "keep-me",
                    }
                },
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {
                            "auto_approve": {"enabled": True},
                            "schedule": {
                                "action": {"enabled": False, "cron": "30 2 * * *"},
                                "review_comments": {"enabled": False},
                            },
                        },
                        sort_keys=False,
                    )
                },
            }
        ),
    )

    report = K8sOperator(runner=runner).config_set("schedule.action.enabled", "true")

    assert report.before is False
    assert report.after is True
    assert report.changed is True
    patch_calls = [call for call in runner.calls if call[0][2:5] == ["patch", "configmap", "agentlab-config"]]
    assert len(patch_calls) == 1
    payload = json.loads(patch_calls[0][0][-1])
    assert set(payload) == {"data"}
    assert "metadata" not in payload
    assert "annotations" not in payload
    updated = yaml.safe_load(payload["data"]["config.yaml"])
    assert updated["auto_approve"]["enabled"] is True
    assert updated["schedule"]["action"] == {"enabled": True, "cron": "30 2 * * *"}
    assert not any("secret" in " ".join(call[0]).lower() for call in runner.calls)


def test_config_set_supports_int_and_string_values() -> None:
    cooldown_runner = FakeRunner()
    cooldown_runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {"schedule": {"review_comments": {"cooldown_minutes": 10}}},
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    cooldown = K8sOperator(runner=cooldown_runner).config_set("schedule.review_comments.cooldown_minutes", "0")

    assert cooldown.before == 10
    assert cooldown.after == 0
    cooldown_payload = json.loads(cooldown_runner.calls[-1][0][-1])
    cooldown_config = yaml.safe_load(cooldown_payload["data"]["config.yaml"])
    assert cooldown_config["schedule"]["review_comments"]["cooldown_minutes"] == 0

    cron_runner = FakeRunner()
    cron_runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {"schedule": {"action": {"cron": "30 2 * * *"}}},
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    cron = K8sOperator(runner=cron_runner).config_set("schedule.action.cron", "15 3 * * *")

    assert cron.before == "30 2 * * *"
    assert cron.after == "15 3 * * *"
    cron_payload = json.loads(cron_runner.calls[-1][0][-1])
    cron_config = yaml.safe_load(cron_payload["data"]["config.yaml"])
    assert cron_config["schedule"]["action"]["cron"] == "15 3 * * *"


def test_config_set_rejects_unknown_path_before_cluster_access() -> None:
    runner = FakeRunner()

    with pytest.raises(K8sOperatorError, match="unsupported config path"):
        K8sOperator(runner=runner).config_set("gitlab_token_env", "GITLAB_TOKEN")

    assert runner.calls == []


def test_config_set_rejects_wrong_value_type_before_cluster_access() -> None:
    runner = FakeRunner()

    with pytest.raises(K8sOperatorError, match="expects a boolean"):
        K8sOperator(runner=runner).config_set("schedule.action.enabled", "maybe")

    assert runner.calls == []


def test_upgrade_preserved_review_comments_creates_cronjob_manifest_and_kustomization(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path, include_review_comments_cronjob=False)
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {
                            "schedule": {
                                "review_comments": {
                                    "enabled": True,
                                    "cron": "*/1 * * * *",
                                }
                            }
                        },
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    report = K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
        image="registry/agentlab:new",
        preserve_cluster_config=True,
    )

    cronjob_path = tmp_path / "cronjob-scheduler-review-comments.yaml"
    assert cronjob_path.exists()
    assert manifest_image(cronjob_path) == "registry/agentlab:new"
    cronjob = yaml.safe_load(cronjob_path.read_text(encoding="utf-8"))
    assert cronjob["metadata"]["name"] == "agentlab-scheduler-review-comments"
    assert cronjob["spec"]["schedule"] == "*/1 * * * *"
    kustomization = yaml.safe_load((tmp_path / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "cronjob-scheduler-review-comments.yaml" in kustomization["resources"]
    assert "cronjob-scheduler-review-comments.yaml" in report.updated_manifests
    assert "kustomization.yaml" in report.updated_manifests


def test_upgrade_fails_when_enabled_cronjob_has_no_generated_job_manifest(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path, include_review_comments_cronjob=False)
    (tmp_path / "job-scheduler-review-comments.yaml").unlink()
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {"schedule": {"review_comments": {"enabled": True}}},
                        sort_keys=False,
                    )
                }
            }
        ),
    )

    with pytest.raises(K8sOperatorError, match="missing generated Job manifest for enabled CronJob review-comments"):
        K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
            image="registry/agentlab:new",
            preserve_cluster_config=True,
        )


def test_upgrade_fails_if_manifest_dir_missing(tmp_path: Path) -> None:
    with pytest.raises(K8sOperatorError, match="manifest dir is missing"):
        K8sOperator(manifest_dir=tmp_path / "missing", runner=FakeRunner()).upgrade(image="registry/agentlab:new")


def test_upgrade_detects_manifest_image_drift_after_update(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    (tmp_path / "custom.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "Job",
                "spec": {"template": {"spec": {"containers": [{"image": "registry/agentlab:old"}]}}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(image="registry/agentlab:new")

    assert report.image_drift == ["custom.yaml: registry/agentlab:old != registry/agentlab:new"]


def test_upgrade_dry_run_does_not_apply_but_apply_does(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()

    K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(image="registry/agentlab:new")

    assert not any(call[0][2:4] == ["apply", "-k"] for call in runner.calls)

    K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(image="registry/agentlab:new", apply=True)

    assert ["-n", "agentlab", "apply", "-k", str(tmp_path)] in [call[0] for call in runner.calls]


def test_upgrade_apply_reapplies_preserved_review_comments_cronjob_and_clears_drift(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path, include_review_comments_cronjob=False)
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        json.dumps(
            {
                "metadata": {"annotations": {K8S_IMAGE_ANNOTATION: "registry/agentlab:new"}},
                "data": {
                    "config.yaml": yaml.safe_dump(
                        {"schedule": {"review_comments": {"enabled": True}}},
                        sort_keys=False,
                    )
                },
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "agentlab-scheduler-review-comments"},
                        "spec": {
                            "schedule": "*/15 * * * *",
                            "jobTemplate": {
                                "spec": {
                                    "template": {
                                        "spec": {"containers": [{"image": "registry/agentlab:new"}]}
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

    report = K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
        image="registry/agentlab:new",
        preserve_cluster_config=True,
        apply=True,
    )

    calls = [call[0] for call in runner.calls]
    assert ["-n", "agentlab", "apply", "-k", str(tmp_path)] in calls
    assert ["-n", "agentlab", "apply", "-f", str(tmp_path / "cronjob-scheduler-review-comments.yaml")] in calls
    assert "cronjob-scheduler-review-comments.yaml" in yaml.safe_load(
        (tmp_path / "kustomization.yaml").read_text(encoding="utf-8")
    )["resources"]
    assert report.image_drift == []


def test_status_detects_review_comments_image_drift() -> None:
    runner = FakeRunner()
    configmap, cronjobs = cronjob_names_json(
        configmap_image="registry/agentlab:new",
        cronjob_images={"agentlab-scheduler-review-comments": "registry/agentlab:old"},
    )
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap)
    runner.respond(["-n", "agentlab", "get", "cronjobs", "-o", "json"], cronjobs)
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner).status()

    assert status.cronjobs[0].name == "agentlab-scheduler-review-comments"
    assert status.cronjobs[0].image_drift is True


def test_status_includes_open_agent_mr_details() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap_with_app_config())
    runner.respond(["-n", "agentlab", "get", "cronjobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner, gitlab_tool_factory=FakeGitLabForStatus).status()
    rendered = format_status(status)

    assert status.open_agent_mrs == [
        {
            "iid": 18,
            "title": "Add smoke baseline",
            "state": "opened",
            "source_branch": "agent/tests-02-smoke-baseline",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/18",
            "labels": ["agent/generated"],
            "updated_at": "2026-05-22T12:00:00Z",
        }
    ]
    assert "Open Agent MRs:" in rendered
    assert "- !18 [agent] Add smoke baseline | branch=agent/tests-02-smoke-baseline | https://gitlab.example.com/group/project/-/merge_requests/18" in rendered


def test_status_keeps_cluster_status_when_open_mr_api_fails_and_redacts_warning() -> None:
    class FailingGitLab:
        def __init__(self, _config) -> None:
            pass

        def list_open_agent_mrs(self) -> list[object]:
            raise RuntimeError("token=super-secret failed")

    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap_with_app_config())
    runner.respond(["-n", "agentlab", "get", "cronjobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    status = K8sOperator(runner=runner, gitlab_tool_factory=FailingGitLab).status()
    rendered = format_status(status)

    assert status.open_agent_mrs == []
    assert "could not read open Agent MRs" in (status.open_agent_mrs_warning or "")
    assert "super-secret" not in rendered
    assert "Warnings:" in rendered


def test_health_summarizes_runtime_scheduler_gitlab_models_and_doctor() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        configmap_with_app_config(
            {
                "ollama": {
                    "base_url": "http://ollama.local:11434",
                    "models": {"default": "qwen-health", "planner": "qwen-plan"},
                },
                "schedule": {
                    "enabled": True,
                    "action": {"enabled": True, "cron": "30 2 * * *"},
                    "review_comments": {
                        "enabled": True,
                        "cron": "*/15 * * * *",
                        "allowed_authors": ["alice"],
                        "require_author_role": ["owner", "maintainer"],
                        "cooldown_minutes": 0,
                        "max_comments_per_run": 2,
                    },
                },
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "cronjobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "agentlab-scheduler-action"},
                        "spec": {
                            "schedule": "30 2 * * *",
                            "jobTemplate": {
                                "spec": {
                                    "template": {
                                        "spec": {
                                            "containers": [{"image": "registry/agentlab:old"}],
                                        }
                                    }
                                }
                            },
                        },
                        "status": {"lastScheduleTime": "2026-05-22T01:00:00Z"},
                    }
                ]
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "agentlab-scheduler-action-failed", "creationTimestamp": "2026-05-22T12:01:00Z"},
                        "status": {"failed": 1},
                    },
                    {
                        "metadata": {"name": "agentlab-doctor", "creationTimestamp": "2026-05-22T12:00:00Z"},
                        "status": {"succeeded": 1},
                    },
                ]
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "agentlab-action-pod"},
                        "status": {"phase": "Failed", "reason": "Error"},
                    }
                ]
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "pod", "artifact-shell", "-o", "json"],
        json.dumps({"metadata": {"name": "artifact-shell"}, "status": {"phase": "Running"}}),
    )
    runner.respond(["-n", "agentlab", "wait", "--for=condition=Ready", "pod/artifact-shell", "--timeout=60s"])
    state_path = "/var/lib/agentlab/runs/scheduler/state.json"
    runner.respond(["-n", "agentlab", "exec", "artifact-shell", "--", "sh", "-c", f"test -f '{state_path}'"])
    runner.respond(["-n", "agentlab", "exec", "artifact-shell", "--", "sh", "-c", f"stat -c %Y '{state_path}'"], "1")
    runner.respond(
        ["-n", "agentlab", "exec", "artifact-shell", "--", "sh", "-c", f"cat '{state_path}'"],
        json.dumps(
            {
                "last_watch_run": "2026-05-22T09:00:00Z",
                "last_plan_run": "2026-05-22T10:00:00Z",
                "last_action_run": "2026-05-22T11:00:00Z",
                "last_review_comment_run": "2026-05-22T12:00:00Z",
                "open_agent_mrs": 1,
                "last_selected_task_id": "tests-02-smoke-baseline",
                "processed_review_comments": {"1:18:41": {"status": "passed"}},
                "closed_agent_mr_feedback": [{"iid": 17}],
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "logs", "job/agentlab-doctor", "--tail=200"],
        "AgentLab doctor: warning\nWARN: Ollama model is not listed: qwen-health\n",
    )

    report = K8sOperator(runner=runner, gitlab_tool_factory=FakeGitLabForStatus).health(manifest_dir=None)
    rendered = format_health(report)

    assert report.status == "failed"
    assert report.images["drift"] == [
        "CronJob agentlab-scheduler-action: registry/agentlab:old != registry/agentlab:new"
    ]
    assert report.failed_resources["jobs"][0]["name"] == "agentlab-scheduler-action-failed"
    assert report.failed_resources["pods"][0]["name"] == "agentlab-action-pod"
    assert report.open_agent_mrs[0]["iid"] == 18
    assert report.gitlab["open_agent_mrs_count"] == 1
    assert report.scheduler["action_enabled"] is True
    assert report.scheduler["review_comments"]["allowed_authors"] == ["alice"]
    assert report.scheduler["last_review_run"] == "2026-05-22T12:00:00Z"
    assert report.scheduler["state_age_seconds"] is not None
    assert report.models["default"] == "qwen-health"
    assert report.doctor["status"] == "warning"
    assert "AgentLab health: failed" in rendered
    assert "Open Agent MRs: 1" in rendered
    assert "- !18 Add smoke baseline | agent/tests-02-smoke-baseline | https://gitlab.example.com/group/project/-/merge_requests/18" in rendered
    assert "review-comments: enabled (authors=alice, roles=owner, maintainer)" in rendered
    assert "super-secret" not in rendered


def test_mrs_reads_configmap_and_secret_then_formats_agent_mrs() -> None:
    FakeGitLabForMrs.seen_token = None
    FakeGitLabForMrs.seen_project_id = None
    FakeGitLabForMrs.seen_args = None
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap_with_app_config())
    runner.respond(["-n", "agentlab", "get", "secret", "agentlab-secrets", "-o", "json"], gitlab_secret())

    report = K8sOperator(runner=runner, gitlab_tool_factory=FakeGitLabForMrs).mrs(
        state="opened",
        label="agent/generated",
    )
    rendered = format_mrs(report)

    assert FakeGitLabForMrs.seen_token == "super-secret"
    assert FakeGitLabForMrs.seen_project_id == 1
    assert FakeGitLabForMrs.seen_args == ("opened", "agent/generated")
    assert report.merge_requests == [
        {
            "iid": 18,
            "title": "Add smoke baseline",
            "state": "opened",
            "source_branch": "agent/tests-02-smoke-baseline",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/18",
            "labels": ["agent/generated"],
            "updated_at": "2026-05-22T12:00:00Z",
        }
    ]
    assert (
        "!18 | Add smoke baseline | opened | agent/tests-02-smoke-baseline | "
        "https://gitlab.example.com/group/project/-/merge_requests/18 | agent/generated"
    ) in rendered
    assert "super-secret" not in rendered
    assert [call[0] for call in runner.calls] == [
        ["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"],
        ["-n", "agentlab", "get", "secret", "agentlab-secrets", "-o", "json"],
    ]


def test_mrs_supports_string_data_secret() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap_with_app_config())
    runner.respond(
        ["-n", "agentlab", "get", "secret", "agentlab-secrets", "-o", "json"],
        json.dumps({"stringData": {"GITLAB_TOKEN": "plain-secret"}}),
    )

    K8sOperator(runner=runner, gitlab_tool_factory=FakeGitLabForMrs).mrs()

    assert FakeGitLabForMrs.seen_token == "plain-secret"


def test_mrs_redacts_secret_from_gitlab_errors() -> None:
    class FailingGitLabForMrs:
        def __init__(self, _config, *, token: str | None = None) -> None:
            self.token = token

        def list_agent_merge_requests(self, *, state: str, label: str) -> list[object]:
            raise RuntimeError(f"token={self.token} failed")

    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "configmap", "agentlab-config", "-o", "json"], configmap_with_app_config())
    runner.respond(["-n", "agentlab", "get", "secret", "agentlab-secrets", "-o", "json"], gitlab_secret())

    with pytest.raises(K8sOperatorError) as excinfo:
        K8sOperator(runner=runner, gitlab_tool_factory=FailingGitLabForMrs).mrs()

    message = str(excinfo.value)
    assert "could not list GitLab merge requests" in message
    assert "super-secret" not in message
    assert "REDACTED" in message


def test_upgrade_run_doctor_and_cleanup_after_successful_apply(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "logs", "job/agentlab-doctor"], "AgentLab doctor: passed\n")
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    report = K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
        image="registry/agentlab:new",
        apply=True,
        run_doctor=True,
        cleanup_failed=True,
    )

    calls = [call[0] for call in runner.calls]
    assert ["-n", "agentlab", "delete", "job", "agentlab-doctor", "--ignore-not-found=true"] in calls
    assert ["-n", "agentlab", "apply", "-f", str(tmp_path / "job-doctor.yaml")] in calls
    assert report.doctor_status == "passed"
    assert report.cleanup_report is not None


def test_upgrade_treats_doctor_warning_as_nonfatal(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "logs", "job/agentlab-doctor"],
        "AgentLab doctor: warning\n- push_agent_branches_enabled is false\n",
    )

    report = K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
        image="registry/agentlab:new",
        apply=True,
        run_doctor=True,
    )

    assert report.doctor_status == "warning"
    assert report.applied is True


def test_doctor_empty_logs_are_retried_until_warning(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-doctor"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stdout="", returncode=0),
            KubectlResult(args=args, stdout="AgentLab doctor: warning\n", returncode=0),
        ],
    )

    report = K8sOperator(
        manifest_dir=tmp_path,
        runner=runner,
        log_retry_delay_seconds=0,
    ).upgrade(
        image="registry/agentlab:new",
        apply=True,
        run_doctor=True,
    )

    assert report.doctor_status == "warning"
    assert [call[0] for call in runner.calls].count(args) == 2


def test_doctor_partial_logs_are_retried_until_warning(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-doctor"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stdout="checking AgentLab configuration...\n", returncode=0),
            KubectlResult(args=args, stdout="checking AgentLab configuration...\nAgentLab doctor: warning\n", returncode=0),
        ],
    )

    report = K8sOperator(
        manifest_dir=tmp_path,
        runner=runner,
        log_retry_delay_seconds=0,
    ).upgrade(
        image="registry/agentlab:new",
        apply=True,
        run_doctor=True,
    )

    assert report.doctor_status == "warning"
    assert [call[0] for call in runner.calls].count(args) == 2


def test_upgrade_treats_doctor_failed_as_fatal(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "logs", "job/agentlab-doctor"], "AgentLab doctor: failed\n")

    with pytest.raises(K8sOperatorError, match="Doctor failed"):
        K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
            image="registry/agentlab:new",
            apply=True,
            run_doctor=True,
        )


def test_doctor_repeated_empty_logs_fail_clearly(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-doctor"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stdout="", returncode=0),
            KubectlResult(args=args, stdout="", returncode=0),
        ],
    )

    with pytest.raises(K8sOperatorError, match="Doctor logs were empty after retries"):
        K8sOperator(
            manifest_dir=tmp_path,
            runner=runner,
            log_retry_attempts=2,
            log_retry_delay_seconds=0,
        ).upgrade(
            image="registry/agentlab:new",
            apply=True,
            run_doctor=True,
        )


def test_doctor_repeated_partial_logs_fail_with_last_snippet(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-doctor"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stdout="checking config\n", returncode=0),
            KubectlResult(args=args, stdout="still checking config\n", returncode=0),
        ],
    )

    with pytest.raises(K8sOperatorError, match="Last log snippet: still checking config"):
        K8sOperator(
            manifest_dir=tmp_path,
            runner=runner,
            log_retry_attempts=2,
            log_retry_delay_seconds=0,
        ).upgrade(
            image="registry/agentlab:new",
            apply=True,
            run_doctor=True,
        )


def test_doctor_fails_when_logs_are_missing_or_unreadable(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "logs", "job/agentlab-doctor"],
        stderr="pods not found",
        returncode=1,
    )

    with pytest.raises(K8sOperatorError, match="pods not found"):
        K8sOperator(manifest_dir=tmp_path, runner=runner).upgrade(
            image="registry/agentlab:new",
            apply=True,
            run_doctor=True,
        )


def test_container_creating_log_error_is_retried(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-doctor"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stderr="container is waiting to start: ContainerCreating", returncode=1),
            KubectlResult(args=args, stdout="AgentLab doctor: warning\n", returncode=0),
        ],
    )

    logs = K8sOperator(
        manifest_dir=tmp_path,
        runner=runner,
        log_retry_delay_seconds=0,
    ).job_logs("agentlab-doctor", follow=False)

    assert logs == "AgentLab doctor: warning\n"
    assert [call[0] for call in runner.calls].count(args) == 2


def test_streaming_action_logs_retry_container_creating(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-scheduler-action", "-f"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stderr='container "agentlab" is waiting to start: ContainerCreating', returncode=1),
            KubectlResult(args=args, stdout="action logs\n", returncode=0),
        ],
    )

    K8sOperator(
        manifest_dir=tmp_path,
        runner=runner,
        log_retry_delay_seconds=0,
    ).job_logs("agentlab-scheduler-action", follow=True)

    assert runner.stream_calls.count(args) == 2


def test_run_component_action_retries_transient_stream_error(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)
    runner = FakeRunner()
    args = ["-n", "agentlab", "logs", "job/agentlab-scheduler-action", "-f"]
    runner.respond_many(
        args,
        [
            KubectlResult(args=args, stderr="pod is PodInitializing", returncode=1),
            KubectlResult(args=args, stdout="action logs\n", returncode=0),
        ],
    )

    K8sOperator(
        manifest_dir=tmp_path,
        runner=runner,
        log_retry_delay_seconds=0,
    ).run_component("action", follow=True)

    assert ["-n", "agentlab", "delete", "job", "agentlab-scheduler-action", "--ignore-not-found=true"] in [
        call[0] for call in runner.calls
    ]
    assert ["-n", "agentlab", "apply", "-f", str(tmp_path / "job-scheduler-action.yaml")] in [
        call[0] for call in runner.calls
    ]
    assert runner.stream_calls.count(args) == 2


def test_format_upgrade_report_mentions_drift() -> None:
    rendered = format_upgrade_report(
        type(
            "Upgrade",
            (),
            {
                "namespace": "agentlab",
                "manifest_dir": "generated",
                "image": "image:new",
                "updated_manifests": ["configmap.yaml"],
                "preserved_sections": ["auto_approve"],
                "apply": True,
                "applied": False,
                "run_doctor": True,
                "doctor_status": "warning",
                "cleanup_failed": True,
                "cleanup_report": None,
                "image_drift": ["cronjob old image"],
            },
        )()
    )

    assert "AgentLab Kubernetes upgrade plan" in rendered
    assert "- image drift: cronjob old image" in rendered
    assert "- Upgrade was not applied because manifest drift/preflight failed." in rendered


def test_failed_agentlab_job_is_selected() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps({"items": [job_item("agentlab-failed", failed=1)]}),
    )
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.jobs == ["agentlab-failed"]


def test_non_agentlab_failed_job_is_ignored() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps({"items": [job_item("other-failed", failed=1)]}),
    )
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.jobs == []
    assert resources.skipped_resources == ["job/other-failed: not an AgentLab resource"]


def test_active_agentlab_job_is_ignored() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps({"items": [job_item("agentlab-active", failed=1, active=1)]}),
    )
    runner.respond(["-n", "agentlab", "get", "pods", "-o", "json"], json.dumps({"items": []}))

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.jobs == []
    assert resources.skipped_resources == ["job/agentlab-active: still active"]


def test_failed_agentlab_pod_is_selected() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps({"items": [pod_item("agentlab-failed-pod", phase="Failed")]}),
    )

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.pods == ["agentlab-failed-pod"]


def test_agentlab_pod_with_error_reason_is_selected() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps({"items": [pod_item("agentlab-error-pod", phase="Unknown", reason="Error")]}),
    )

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.pods == ["agentlab-error-pod"]


def test_running_agentlab_pod_is_ignored() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps({"items": [pod_item("agentlab-running-pod", phase="Running", reason="Error")]}),
    )

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.pods == []
    assert resources.skipped_resources == ["pod/agentlab-running-pod: still running"]


def test_completed_pod_without_failure_is_ignored() -> None:
    runner = FakeRunner()
    runner.respond(["-n", "agentlab", "get", "jobs", "-o", "json"], json.dumps({"items": []}))
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps({"items": [pod_item("agentlab-complete-pod", phase="Succeeded", reason="Completed")]}),
    )

    resources = K8sOperator(runner=runner).failed_resources()

    assert resources.pods == []


def test_cleanup_failed_dry_run_does_not_delete() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps({"items": [job_item("agentlab-failed", failed=1)]}),
    )
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps({"items": [pod_item("agentlab-failed-pod", phase="Failed")]}),
    )

    report = K8sOperator(runner=runner).cleanup_failed(dry_run=True)

    assert report.dry_run is True
    assert report.deleted_jobs == ["agentlab-failed"]
    assert report.deleted_pods == ["agentlab-failed-pod"]
    assert not any(call[0][2:4] == ["delete", "job"] for call in runner.calls)
    assert not any(call[0][2:4] == ["delete", "pod"] for call in runner.calls)


def test_cleanup_failed_deletes_only_selected_agentlab_jobs_and_pods() -> None:
    runner = FakeRunner()
    runner.respond(
        ["-n", "agentlab", "get", "jobs", "-o", "json"],
        json.dumps(
            {
                "items": [
                    job_item("agentlab-failed", failed=1),
                    job_item("agentlab-active", failed=1, active=1),
                    job_item("other-failed", failed=1),
                ]
            }
        ),
    )
    runner.respond(
        ["-n", "agentlab", "get", "pods", "-o", "json"],
        json.dumps(
            {
                "items": [
                    pod_item("agentlab-failed-pod", phase="Failed"),
                    pod_item("agentlab-running-pod", phase="Running"),
                    pod_item("other-failed-pod", phase="Failed"),
                ]
            }
        ),
    )

    report = K8sOperator(runner=runner).cleanup_failed()

    assert report.deleted_jobs == ["agentlab-failed"]
    assert report.deleted_pods == ["agentlab-failed-pod"]
    delete_calls = [call[0] for call in runner.calls if "delete" in call[0]]
    assert delete_calls == [
        ["-n", "agentlab", "delete", "job", "agentlab-failed", "--ignore-not-found=true"],
        ["-n", "agentlab", "delete", "pod", "agentlab-failed-pod", "--ignore-not-found=true"],
    ]
    assert not any(resource in call for call in delete_calls for resource in ("cronjob", "pvc", "configmap", "secret", "serviceaccount"))


def test_tui_selector_accepts_numbers_and_exact_text() -> None:
    choices = ["watch", "plan", "action", "review-comments", "doctor"]

    assert resolve_tui_choice("1", choices) == "watch"
    assert resolve_tui_choice("5", choices) == "doctor"
    assert resolve_tui_choice("doctor", choices) == "doctor"
    assert resolve_tui_choice("review-comments", choices) == "review-comments"


def test_tui_selector_accepts_display_labels_and_case_insensitive_input() -> None:
    choices = [TuiChoice("status", "Status anzeigen"), TuiChoice("quit", "Beenden", aliases=("exit",))]

    assert resolve_tui_choice("STATUS ANZEIGEN", choices) == "status"
    assert resolve_tui_choice("exit", choices) == "quit"


def test_tui_selector_invalid_input_lists_valid_choices() -> None:
    with pytest.raises(K8sOperatorError) as excinfo:
        resolve_tui_choice("wat", ["watch", "plan", "action", "review-comments", "doctor"])

    assert "Invalid selection: wat" in str(excinfo.value)
    assert "Valid choices: 1-5 or watch, plan, action, review-comments, doctor" in str(excinfo.value)


def test_fallback_confirm_defaults_match_prompt() -> None:
    yes_prompts: list[str] = []
    yes_adapter = FallbackTUIAdapter(input_func=lambda prompt: yes_prompts.append(prompt) or "", output_func=lambda _text: None)

    assert yes_adapter.confirm("Create artifact-shell pod if missing?", default=True) is True
    assert yes_prompts == ["Create artifact-shell pod if missing? [Y/n] "]

    no_prompts: list[str] = []
    no_adapter = FallbackTUIAdapter(input_func=lambda prompt: no_prompts.append(prompt) or "", output_func=lambda _text: None)

    assert no_adapter.confirm("Apply generated manifests to the cluster?", default=False) is False
    assert no_prompts == ["Apply generated manifests to the cluster? [y/N] "]


def test_questionary_adapter_select_confirm_and_text() -> None:
    class FakeStyle:
        def __init__(self, rules):
            self.rules = rules

    class FakePrompt:
        def __init__(self, answer):
            self.answer = answer

        def ask(self):
            return self.answer

    class FakeQuestionary:
        Style = FakeStyle

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def select(self, message, **kwargs):
            self.calls.append(("select", (message, kwargs)))
            return FakePrompt("Doctor")

        def confirm(self, message, **kwargs):
            self.calls.append(("confirm", (message, kwargs)))
            return FakePrompt(None)

        def text(self, message, **kwargs):
            self.calls.append(("text", (message, kwargs)))
            return FakePrompt("")

    questionary = FakeQuestionary()
    adapter = QuestionaryTUIAdapter(questionary)

    assert adapter.select("Component", [TuiChoice("doctor", "Doctor")]) == "doctor"
    assert adapter.confirm("Run doctor after apply?", default=True) is True
    assert adapter.text("Run ID", default="latest") == "latest"

    assert adapter.style is not None
    assert adapter.style.rules == QUESTIONARY_TUI_STYLE_RULES
    assert questionary.calls[0][1][1]["style"] is adapter.style
    assert questionary.calls[1][1][1]["style"] is adapter.style
    assert questionary.calls[2][1][1]["style"] is adapter.style


def test_questionary_tui_style_highlights_current_row_without_selected_default() -> None:
    class FakeStyle:
        def __init__(self, rules):
            self.rules = dict(rules)

    class FakeQuestionary:
        Style = FakeStyle

    style = build_questionary_tui_style(FakeQuestionary)
    required_keys = {
        "highlighted",
        "selected",
        "pointer",
        "answer",
        "text",
        "instruction",
        "qmark",
        "question",
        "checkbox",
        "separator",
        "disabled",
        "shortcut",
    }

    assert style.rules["pointer"] == "bold"
    assert style.rules["highlighted"] == "bold fg:#ffffff bg:#444444"
    assert style.rules["selected"] == ""
    assert required_keys.issubset(style.rules)
    assert all("reverse" not in rule.lower() for rule in style.rules.values())


def test_questionary_select_does_not_pass_default_to_avoid_stale_highlight() -> None:
    class FakeStyle:
        def __init__(self, rules):
            self.rules = dict(rules)

    class FakePrompt:
        def ask(self):
            return "Artifact ansehen"

    class FakeQuestionary:
        Style = FakeStyle

        def __init__(self) -> None:
            self.select_kwargs: dict[str, object] | None = None

        def select(self, message, **kwargs):
            self.select_kwargs = kwargs
            return FakePrompt()

    questionary = FakeQuestionary()
    adapter = QuestionaryTUIAdapter(questionary)

    selected = adapter.select(
        "Auswahl",
        [
            TuiChoice("status", "Status anzeigen"),
            TuiChoice("artifact", "Artifact ansehen"),
        ],
        default="status",
    )

    assert selected == "artifact"
    assert questionary.select_kwargs is not None
    assert "default" not in questionary.select_kwargs


def test_create_tui_adapter_missing_questionary_prints_install_hint_once(monkeypatch) -> None:
    def fake_import_module(name: str):
        assert name == "questionary"
        raise ImportError(name)

    output: list[str] = []
    monkeypatch.setattr("agentlab.k8s_operator.importlib.import_module", fake_import_module)

    adapter = create_tui_adapter(input_func=lambda _prompt: "quit", output_func=output.append)

    assert isinstance(adapter, FallbackTUIAdapter)
    assert output == [QUESTIONARY_TUI_INSTALL_HINT]


def test_tui_artifact_shell_prompt_defaults_to_yes() -> None:
    operator = FakeOperator()
    answers = iter(["", "", "1"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
    )

    tui.run_once("artifact")

    assert ("artifact", ("e474a44a82dc4bf8b6b8ce2732194ffc", "manifest.json")) in operator.calls


def test_tui_delete_failed_resources_defaults_to_no() -> None:
    operator = FakeOperator()
    answers = iter([""])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
    )

    tui.run_once("cleanup")

    assert operator.calls == [("failed_resources", None)]


def test_tui_logs_accepts_doctor_text_selection() -> None:
    operator = FakeOperator()
    answers = iter(["doctor"])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
    )

    tui.run_once("logs")

    assert operator.calls == [
        ("latest_job_name", "doctor"),
        ("job_logs", ("agentlab-doctor", False, None)),
    ]
    assert "Selected Job: agentlab-doctor" in output


def test_tui_logs_uses_arrow_key_adapter_selection() -> None:
    class SelectDoctorAdapter:
        def select(self, label: str, choices: list[str | TuiChoice], *, default: str | None = None) -> str:
            return "doctor"

        def confirm(self, message: str, *, default: bool = False) -> bool:
            return default

        def text(self, label: str, *, default: str | None = None) -> str:
            return default or ""

    operator = FakeOperator()
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        output_func=lambda _text: None,
        adapter=SelectDoctorAdapter(),
    )

    tui.run_once("logs")

    assert operator.calls == [
        ("latest_job_name", "doctor"),
        ("job_logs", ("agentlab-doctor", False, None)),
    ]


def test_tui_logs_accepts_doctor_numeric_selection() -> None:
    operator = FakeOperator()
    answers = iter(["5"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
    )

    tui.run_once("3")

    assert operator.calls == [
        ("latest_job_name", "doctor"),
        ("job_logs", ("agentlab-doctor", False, None)),
    ]


def test_tui_artifact_empty_run_id_defaults_to_latest() -> None:
    operator = FakeOperator()
    answers = iter(["", "gate_decision.json"])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: True,
    )

    tui.run_once("artifact")

    assert operator.calls == [
        ("ensure_artifact_shell", None),
        ("runs", None),
        ("latest_run_id", None),
        ("available_artifacts", "e474a44a82dc4bf8b6b8ce2732194ffc"),
        ("artifact", ("e474a44a82dc4bf8b6b8ce2732194ffc", "gate_decision.json")),
    ]
    assert "Run ID: e474a44a82dc4bf8b6b8ce2732194ffc" in output
    assert "Available artifacts:" in output


def test_tui_artifact_numeric_selection_reads_selected_artifact() -> None:
    operator = FakeOperator()
    answers = iter(["latest", "2"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
        confirm_func=lambda _message: True,
    )

    tui.run_once("artifact")

    assert ("artifact", ("e474a44a82dc4bf8b6b8ce2732194ffc", "gate_decision.json")) in operator.calls


def test_tui_artifact_empty_artifact_name_does_not_call_operator_artifact() -> None:
    operator = FakeOperator()
    answers = iter(["latest", ""])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: True,
    )

    tui.run_once("artifact")

    assert not any(call[0] == "artifact" for call in operator.calls)
    assert "Artifact name is required." in output


def test_tui_artifact_no_available_artifacts_returns_to_menu() -> None:
    class EmptyArtifactOperator(FakeOperator):
        def available_artifacts(self, run_id: str):
            self.calls.append(("available_artifacts", run_id))
            return []

    operator = EmptyArtifactOperator()
    answers = iter(["latest"])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: True,
    )

    tui.run_once("artifact")

    assert not any(call[0] == "artifact" for call in operator.calls)
    assert "No artifacts found for run e474a44a82dc4bf8b6b8ce2732194ffc" in output


def test_tui_artifact_missing_prints_available_artifacts_and_returns_to_menu() -> None:
    class MissingArtifactOperator(FakeOperator):
        def available_artifacts(self, run_id: str):
            self.calls.append(("available_artifacts", run_id))
            return ["missing.json", "gate_decision.json"]

        def artifact(self, run_id: str, artifact: str):
            self.calls.append(("artifact", (run_id, artifact)))
            raise ArtifactNotFoundError("/runs/latest/artifacts/missing.json", ["gate_decision.json", "proposed.diff"])

    operator = MissingArtifactOperator()
    answers = iter(["", "missing.json"])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: True,
    )

    assert tui.run_once("5") is True

    assert operator.calls == [
        ("ensure_artifact_shell", None),
        ("runs", None),
        ("latest_run_id", None),
        ("available_artifacts", "e474a44a82dc4bf8b6b8ce2732194ffc"),
        ("artifact", ("e474a44a82dc4bf8b6b8ce2732194ffc", "missing.json")),
    ]
    assert any("artifact not found" in line for line in output)
    assert "- gate_decision.json" in output
    assert "- proposed.diff" in output


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


def test_tui_cleanup_failed_maps_to_same_helper() -> None:
    operator = FakeOperator()
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: "",
        output_func=lambda _text: None,
        confirm_func=lambda _message: True,
    )

    tui.run_once("11")

    assert operator.calls == [
        ("failed_resources", None),
        ("cleanup_failed", False),
    ]


def test_tui_upgrade_action_calls_same_upgrade_helper() -> None:
    operator = FakeOperator()
    answers = iter(["registry/agentlab:new", "2"])
    confirmations = iter([True, True, True, True])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
        confirm_func=lambda _message: next(confirmations),
    )

    tui.run_once("10")

    assert operator.calls == [
        (
            "upgrade",
            {
                "image": "registry/agentlab:new",
                "apply": True,
                "preserve_local_config": True,
                "preserve_cluster_config": False,
                "run_doctor": True,
                "cleanup_failed": True,
            },
        )
    ]


@pytest.mark.parametrize("image", ["", "   "])
def test_tui_upgrade_empty_image_does_not_call_operator(image: str) -> None:
    operator = FakeOperator()
    answers = iter([image])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: pytest.fail("upgrade follow-up prompt should not be shown"),
    )

    tui.run_once("upgrade")

    assert operator.calls == []
    assert output == ["Image is required. Upgrade cancelled."]


def test_tui_upgrade_valid_image_without_apply_calls_operator() -> None:
    operator = FakeOperator()
    answers = iter(["registry/agentlab:new", "cluster config"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
        confirm_func=lambda _message: False,
    )

    tui.run_once("upgrade")

    assert operator.calls == [
        (
            "upgrade",
            {
                "image": "registry/agentlab:new",
                "apply": False,
                "preserve_local_config": False,
                "preserve_cluster_config": True,
                "run_doctor": False,
                "cleanup_failed": False,
            },
        )
    ]


def test_tui_upgrade_apply_defaults_to_no_and_skips_apply_followups() -> None:
    operator = FakeOperator()
    answers = iter(["registry/agentlab:new", "1", ""])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
    )

    tui.run_once("upgrade")

    assert operator.calls == [
        (
            "upgrade",
            {
                "image": "registry/agentlab:new",
                "apply": False,
                "preserve_local_config": False,
                "preserve_cluster_config": False,
                "run_doctor": False,
                "cleanup_failed": False,
            },
        )
    ]


def test_tui_upgrade_confirmed_apply_uses_default_yes_for_doctor_and_cleanup() -> None:
    operator = FakeOperator()
    answers = iter(["registry/agentlab:new", "1", "y", "", "", "y"])
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=lambda _text: None,
    )

    tui.run_once("upgrade")

    assert operator.calls == [
        (
            "upgrade",
            {
                "image": "registry/agentlab:new",
                "apply": True,
                "preserve_local_config": False,
                "preserve_cluster_config": False,
                "run_doctor": True,
                "cleanup_failed": True,
            },
        )
    ]


def test_tui_upgrade_apply_declined_confirmation_does_not_call_operator() -> None:
    operator = FakeOperator()
    answers = iter(["registry/agentlab:new", "1"])
    confirmations = iter([True, False, False, False])
    output: list[str] = []
    tui = K8sTUI(
        operator,  # type: ignore[arg-type]
        input_func=lambda _prompt: next(answers),
        output_func=output.append,
        confirm_func=lambda _message: next(confirmations),
    )

    tui.run_once("10")

    assert operator.calls == []
    assert "Upgrade will apply generated manifests to the cluster." in output
    assert "Upgrade cancelled." in output


def test_tui_ctrl_c_exits_without_traceback() -> None:
    output: list[str] = []
    tui = K8sTUI(
        FakeOperator(),  # type: ignore[arg-type]
        input_func=lambda _prompt: (_ for _ in ()).throw(KeyboardInterrupt()),
        output_func=output.append,
    )

    assert tui.run_once("logs") is True

    assert output[-1] == "Cancelled."
    assert not any("Traceback" in line for line in output)


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
