from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentlab.k8s_operator import (
    ArtifactNotFoundError,
    FailedResources,
    K8sOperator,
    K8sOperatorError,
    K8sTUI,
    KubectlResult,
    TuiUnavailableError,
    artifact_path,
    cronjob_for_component,
    detect_manifest_image_drift,
    format_upgrade_report,
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
                    "annotations": {"agentlab.io/image": image},
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
    configmap = json.dumps({"metadata": {"annotations": {"agentlab.io/image": configmap_image}}})
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


def test_upgrade_updates_configmap_annotation_and_all_workload_images(tmp_path: Path) -> None:
    write_upgrade_manifests(tmp_path)

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(image="registry/agentlab:new")

    configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    assert configmap["metadata"]["annotations"]["agentlab.io/image"] == "registry/agentlab:new"
    for path in [*tmp_path.glob("job-*.yaml"), *tmp_path.glob("cronjob-*.yaml")]:
        assert manifest_image(path) == "registry/agentlab:new"
    assert "job-scheduler-reset-state.yaml" in report.updated_manifests
    assert "cronjob-scheduler-review-comments.yaml" in report.updated_manifests
    assert report.image_drift == []


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
    configmap["metadata"]["annotations"]["agentlab.io/image"] = "registry/agentlab:old"
    configmap["data"]["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
    (tmp_path / "configmap.yaml").write_text(yaml.safe_dump(configmap, sort_keys=False), encoding="utf-8")

    report = K8sOperator(manifest_dir=tmp_path, runner=FakeRunner()).upgrade(
        image="registry/agentlab:new",
        preserve_local_config=True,
    )

    new_configmap = yaml.safe_load((tmp_path / "configmap.yaml").read_text(encoding="utf-8"))
    merged = yaml.safe_load(new_configmap["data"]["config.yaml"])
    assert new_configmap["metadata"]["annotations"]["agentlab.io/image"] == "registry/agentlab:new"
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
                "metadata": {"annotations": {"agentlab.io/image": "registry/agentlab:new"}},
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
                            "schedule": "*/10 * * * *",
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
    confirmations = iter([True, True, True])
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
