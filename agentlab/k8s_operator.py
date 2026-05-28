from __future__ import annotations

import base64
import importlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

import yaml

from agentlab.audit import redact_secrets
from agentlab.config import AppConfig
from agentlab.review_comments import normalize_mr
from agentlab.tools.gitlab_tool import GitLabTool


DEFAULT_NAMESPACE = "agentlab"
DEFAULT_MANIFEST_DIR = Path("deploy/kubernetes/generated")
RUNS_ROOT = "/var/lib/agentlab/runs"
ARTIFACT_SHELL_IMAGE = "busybox:1.36"
K8S_IMAGE_ANNOTATION = "mr-robot-ops.github.io/agentlab-image"
K8S_VERSION_ANNOTATION = "mr-robot-ops.github.io/agentlab-version"
DEPRECATED_K8S_IMAGE_ANNOTATION = "agentlab.io/image"
DEPRECATED_K8S_IMAGE_ANNOTATION_WARNING = (
    f"Deprecated annotation key `{DEPRECATED_K8S_IMAGE_ANNOTATION}` found; "
    f"use `{K8S_IMAGE_ANNOTATION}`."
)

JOB_PREFIXES = {
    "review-comments": "agentlab-scheduler-review-comments",
    "action": "agentlab-scheduler-action",
    "plan": "agentlab-scheduler-plan",
    "watch": "agentlab-scheduler-watch",
    "doctor": "agentlab-doctor",
}

RUN_MANIFESTS = {
    "review-comments": "job-scheduler-review-comments.yaml",
    "action": "job-scheduler-action.yaml",
    "plan": "job-scheduler-plan.yaml",
    "watch": "job-scheduler-watch.yaml",
    "doctor": "job-doctor.yaml",
    "reset-state": "job-scheduler-reset-state.yaml",
}

RUN_JOB_NAMES = {
    "review-comments": "agentlab-scheduler-review-comments",
    "action": "agentlab-scheduler-action",
    "plan": "agentlab-scheduler-plan",
    "watch": "agentlab-scheduler-watch",
    "doctor": "agentlab-doctor",
    "reset-state": "agentlab-scheduler-reset-state",
}

CRONJOBS = {
    "review-comments": "agentlab-scheduler-review-comments",
    "action": "agentlab-scheduler-action",
    "plan": "agentlab-scheduler-plan",
    "watch": "agentlab-scheduler-watch",
}

CRONJOB_MANIFESTS = {
    "review-comments": "cronjob-scheduler-review-comments.yaml",
    "action": "cronjob-scheduler-action.yaml",
    "plan": "cronjob-scheduler-plan.yaml",
    "watch": "cronjob-scheduler-watch.yaml",
}

CRONJOB_DEFAULT_CRONS = {
    "review-comments": "*/15 * * * *",
    "action": "30 2 * * *",
    "plan": "0 7,19 * * *",
    "watch": "*/30 * * * *",
}
DEFAULT_JOB_BACKOFF_LIMIT = 0
DEFAULT_JOB_ACTIVE_DEADLINE_SECONDS = 3600
DEFAULT_JOB_TTL_SECONDS_AFTER_FINISHED = 86400
DEFAULT_JOB_RESOURCES = {
    "requests": {"cpu": "250m", "memory": "512Mi"},
    "limits": {"cpu": "1", "memory": "2Gi"},
}
RUNTIME_PATH = "/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin"

CONFIG_SETTING_SPECS: dict[str, tuple[type[bool] | type[int] | type[str], int | None]] = {
    "schedule.action.enabled": (bool, None),
    "schedule.action.cron": (str, None),
    "schedule.review_comments.enabled": (bool, None),
    "schedule.review_comments.cron": (str, None),
    "schedule.review_comments.cooldown_minutes": (int, 0),
    "schedule.review_comments.max_comments_per_run": (int, 1),
    "schedule.plan.enabled": (bool, None),
    "schedule.watch.enabled": (bool, None),
}
CONFIG_SETTING_PATHS = tuple(CONFIG_SETTING_SPECS)


class K8sOperatorError(RuntimeError):
    pass


class MissingManifestError(K8sOperatorError):
    pass


class ArtifactNotFoundError(K8sOperatorError):
    def __init__(self, path: str, available_artifacts: list[str]) -> None:
        self.path = path
        self.available_artifacts = available_artifacts
        available = ", ".join(available_artifacts) if available_artifacts else "none"
        super().__init__(f"artifact not found: {path}. Available artifacts: {available}")


class TuiUnavailableError(K8sOperatorError):
    pass


class TuiCancelled(K8sOperatorError):
    pass


@dataclass(frozen=True)
class TuiChoice:
    value: str
    label: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return self.label or self.value


@dataclass
class KubectlResult:
    args: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class KubectlRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> KubectlResult:
        ...

    def stream(self, args: list[str]) -> KubectlResult:
        ...

    def interactive(self, args: list[str]) -> int:
        ...


class SubprocessKubectlRunner:
    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> KubectlResult:
        command = ["kubectl", *args]
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=capture,
            check=False,
        )
        result = KubectlResult(
            args=args,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
        )
        if check and result.returncode != 0:
            raise K8sOperatorError(result.stderr.strip() or result.stdout.strip() or f"kubectl failed: {' '.join(args)}")
        return result

    def stream(self, args: list[str]) -> KubectlResult:
        process = subprocess.Popen(
            ["kubectl", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        threads: list[threading.Thread] = []
        if process.stdout is not None:
            threads.append(
                threading.Thread(
                    target=_tee_stream,
                    args=(process.stdout, sys.stdout, stdout_chunks),
                    daemon=True,
                )
            )
        if process.stderr is not None:
            threads.append(
                threading.Thread(
                    target=_tee_stream,
                    args=(process.stderr, sys.stderr, stderr_chunks),
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        returncode = process.wait()
        for thread in threads:
            thread.join()
        return KubectlResult(
            args=args,
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            returncode=returncode,
        )

    def interactive(self, args: list[str]) -> int:
        return subprocess.run(["kubectl", *args], check=False).returncode


def _tee_stream(source: TextIO, target: TextIO, chunks: list[str]) -> None:
    try:
        for line in iter(source.readline, ""):
            chunks.append(line)
            target.write(line)
            target.flush()
    finally:
        source.close()


@dataclass
class CronJobStatus:
    name: str
    schedule: str
    suspend: bool
    active: int
    last_schedule: str
    image: str
    image_drift: bool = False


@dataclass
class JobStatus:
    name: str
    created_at: str
    status: str


@dataclass
class PodStatus:
    name: str
    phase: str
    reason: str


@dataclass
class ManifestImageDrift:
    path: str
    image: str
    expected_image: str


@dataclass
class ClusterStatus:
    namespace: str
    configmap_image: str | None
    configmap_version: str | None = None
    image_annotation_warning: str | None = None
    open_agent_mrs: list[dict[str, Any]] = field(default_factory=list)
    open_agent_mrs_count: int | None = None
    open_agent_mrs_warning: str | None = None
    cronjobs: list[CronJobStatus] = field(default_factory=list)
    recent_jobs: list[JobStatus] = field(default_factory=list)
    failed_jobs: list[JobStatus] = field(default_factory=list)
    failed_pods: list[PodStatus] = field(default_factory=list)
    manifest_configmap_image: str | None = None
    manifest_configmap_version: str | None = None
    manifest_image_annotation_warning: str | None = None
    manifest_image_drifts: list[ManifestImageDrift] = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: str
    mtime: str
    status: str = "unknown"
    reason: str = ""


@dataclass
class ArtifactResult:
    run_id: str
    path: str
    content: str


@dataclass
class FailedResources:
    jobs: list[str] = field(default_factory=list)
    pods: list[str] = field(default_factory=list)
    skipped_resources: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.jobs or self.pods)


@dataclass
class CleanupReport:
    namespace: str
    deleted_jobs: list[str] = field(default_factory=list)
    deleted_pods: list[str] = field(default_factory=list)
    skipped_resources: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class UpgradeReport:
    namespace: str
    manifest_dir: str
    image: str
    version: str | None = None
    updated_manifests: list[str] = field(default_factory=list)
    preserved_sections: list[str] = field(default_factory=list)
    apply: bool = False
    applied: bool = False
    run_doctor: bool = False
    doctor_status: str = "not requested"
    cleanup_failed: bool = False
    cleanup_report: CleanupReport | None = None
    status_checked: bool = False
    image_drift: list[str] = field(default_factory=list)


@dataclass
class ConfigValueReport:
    path: str
    value: Any = None
    exists: bool = True


@dataclass
class ConfigSetReport:
    path: str
    before: Any = None
    after: Any = None
    before_exists: bool = True
    changed: bool = False


@dataclass
class MergeRequestListReport:
    namespace: str
    state: str
    label: str
    merge_requests: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HealthReport:
    namespace: str
    status: str
    images: dict[str, Any]
    failed_resources: dict[str, Any]
    open_agent_mrs: list[dict[str, Any]]
    gitlab: dict[str, Any]
    scheduler: dict[str, Any]
    models: dict[str, Any]
    doctor: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class K8sOperator:
    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        manifest_dir: str | Path = DEFAULT_MANIFEST_DIR,
        runner: KubectlRunner | None = None,
        gitlab_tool_factory: Callable[[AppConfig], Any] | None = None,
        log_retry_attempts: int = 5,
        log_retry_delay_seconds: float = 1.0,
    ) -> None:
        self.namespace = namespace
        self.manifest_dir = Path(manifest_dir)
        self.runner = runner or SubprocessKubectlRunner()
        self.gitlab_tool_factory = gitlab_tool_factory or GitLabTool
        self.log_retry_attempts = log_retry_attempts
        self.log_retry_delay_seconds = log_retry_delay_seconds

    def status(self, *, manifest_dir: str | Path | None = None) -> ClusterStatus:
        configmap = self._run_json(["get", "configmap", "agentlab-config", "-o", "json"], check=False)
        configmap_image = _configmap_image(configmap)
        configmap_version = _configmap_version(configmap)
        image_annotation_warning = _configmap_image_annotation_warning(configmap)
        open_agent_mrs, open_agent_mrs_warning = self._open_agent_mrs_from_configmap(configmap)
        cronjobs = [_cronjob_status(item, configmap_image) for item in self._items("cronjobs")]
        cronjobs = [item for item in cronjobs if item.name.startswith("agentlab-scheduler-")]

        jobs = [_job_status(item) for item in self._items("jobs")]
        agentlab_jobs = [job for job in jobs if _is_agentlab_job(job.name)]
        agentlab_jobs.sort(key=lambda item: item.created_at, reverse=True)
        failed_jobs = [job for job in agentlab_jobs if job.status == "failed"]

        failed_pods = [_pod_status(item) for item in self._items("pods")]
        failed_pods = [pod for pod in failed_pods if pod.phase == "Failed" or pod.reason]

        status = ClusterStatus(
            namespace=self.namespace,
            configmap_image=configmap_image,
            configmap_version=configmap_version,
            image_annotation_warning=image_annotation_warning,
            open_agent_mrs=open_agent_mrs,
            open_agent_mrs_count=None if open_agent_mrs_warning else len(open_agent_mrs),
            open_agent_mrs_warning=open_agent_mrs_warning,
            cronjobs=cronjobs,
            recent_jobs=agentlab_jobs[:10],
            failed_jobs=failed_jobs,
            failed_pods=failed_pods,
        )
        if manifest_dir is not None:
            manifest_path = Path(manifest_dir) / "configmap.yaml"
            status.manifest_configmap_image, status.manifest_image_drifts = detect_manifest_image_drift(Path(manifest_dir))
            if manifest_path.exists():
                manifest_configmap = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                status.manifest_configmap_version = _configmap_version(manifest_configmap)
                status.manifest_image_annotation_warning = _configmap_image_annotation_warning(manifest_configmap)
        return status

    def latest_job_name(self, component: str) -> str:
        prefixes = list(JOB_PREFIXES.values()) if component == "latest" else [job_prefix_for_component(component)]
        jobs = [_job_status(item) for item in self._items("jobs")]
        matches = [job for job in jobs if any(job.name.startswith(prefix) for prefix in prefixes)]
        matches.sort(key=lambda item: item.created_at, reverse=True)
        if not matches:
            raise K8sOperatorError(f"no jobs found for component: {component}")
        return matches[0].name

    def logs(self, component: str, *, follow: bool = True, tail: int | None = None) -> tuple[str, str]:
        job_name = self.latest_job_name(component)
        return job_name, self.job_logs(job_name, follow=follow, tail=tail)

    def job_logs(self, job_name: str, *, follow: bool = True, tail: int | None = None) -> str:
        args = ["logs", f"job/{job_name}"]
        if tail is not None:
            args.append(f"--tail={tail}")
        if follow:
            return self._stream_with_retry(args)
        return self._logs_with_retry(args)

    def run_component(
        self,
        component: str,
        *,
        follow: bool = True,
        task_id: str | None = None,
        extra_args: list[str] | None = None,
    ) -> str:
        manifest = manifest_for_component(component, self.manifest_dir)
        if not manifest.exists():
            raise MissingManifestError(
                f"missing manifest: {manifest}. Re-run Kubernetes bootstrap to generate {manifest.name}."
            )
        if task_id is not None and component != "action":
            raise K8sOperatorError("--task-id is only supported for the action component")
        if extra_args and component != "plan":
            raise K8sOperatorError("extra planning arguments are only supported for the plan component")
        job_name = run_job_name_for_component(component)
        self._run(["delete", "job", job_name, "--ignore-not-found=true"])
        if task_id is not None or extra_args:
            self._run(["apply", "-f", "-"], input_text=_manifest_with_args(manifest, task_id=task_id, extra_args=extra_args or []))
        else:
            self._run(["apply", "-f", str(manifest)])
        if follow:
            self.job_logs(job_name, follow=True)
        return str(manifest)

    def run_doctor_job(self) -> str:
        self.run_component("doctor", follow=False)
        logs = self._doctor_logs_with_status_retry(run_job_name_for_component("doctor"))
        return _doctor_status_from_logs(logs)

    def ensure_artifact_shell(self, *, pvc: str = "agentlab-runs", shell_pod: str = "artifact-shell") -> None:
        pod = self._run_json(["get", "pod", shell_pod, "-o", "json"], check=False)
        if not pod:
            self._run(["apply", "-f", "-"], input_text=render_artifact_shell_pod(self.namespace, pvc, shell_pod))
        self._run(["wait", "--for=condition=Ready", f"pod/{shell_pod}", "--timeout=60s"])

    def artifact(
        self,
        run_id: str,
        artifact: str,
        *,
        pvc: str = "agentlab-runs",
        shell_pod: str = "artifact-shell",
    ) -> ArtifactResult:
        self.ensure_artifact_shell(pvc=pvc, shell_pod=shell_pod)
        resolved_run_id = self.latest_run_id(shell_pod=shell_pod) if run_id == "latest" else _safe_name(run_id, "run_id")
        artifact = _safe_artifact_path(artifact)
        path = artifact_path(resolved_run_id, artifact)
        if self._exec(shell_pod, f"test -f {_sh_quote(path)}", check=False).returncode != 0:
            available = self.available_artifacts(resolved_run_id, shell_pod=shell_pod)
            raise ArtifactNotFoundError(path, available)
        content = self._exec(shell_pod, f"cat {_sh_quote(path)}").stdout
        return ArtifactResult(run_id=resolved_run_id, path=path, content=content)

    def latest_run_id(self, *, shell_pod: str = "artifact-shell") -> str:
        command = f"ls -1t {_sh_quote(RUNS_ROOT)} 2>/dev/null | grep -v '^scheduler$' | head -n 1"
        run_id = self._exec(shell_pod, command).stdout.strip()
        if not run_id:
            raise K8sOperatorError("no run directories found")
        return _safe_name(run_id, "run_id")

    def available_artifacts(self, run_id: str, *, shell_pod: str = "artifact-shell") -> list[str]:
        run_id = _safe_name(run_id, "run_id")
        artifacts_dir = f"{RUNS_ROOT}/{run_id}/artifacts"
        result = self._exec(shell_pod, f"ls -1 {_sh_quote(artifacts_dir)} 2>/dev/null || true")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def runs(self, *, limit: int = 20, pvc: str = "agentlab-runs", shell_pod: str = "artifact-shell") -> list[RunSummary]:
        self.ensure_artifact_shell(pvc=pvc, shell_pod=shell_pod)
        command = f"ls -1t {_sh_quote(RUNS_ROOT)} 2>/dev/null | grep -v '^scheduler$' | head -n {int(limit)}"
        result = self._exec(shell_pod, command)
        runs = [_safe_name(line.strip(), "run_id") for line in result.stdout.splitlines() if line.strip()]
        summaries: list[RunSummary] = []
        for run_id in runs:
            mtime = self._exec(shell_pod, f"stat -c %y {_sh_quote(f'{RUNS_ROOT}/{run_id}')} 2>/dev/null || echo unknown").stdout.strip()
            status, reason = self._run_artifact_status(run_id, shell_pod=shell_pod)
            summaries.append(RunSummary(run_id=run_id, mtime=mtime, status=status, reason=reason))
        return summaries

    def shell(self, *, pvc: str = "agentlab-runs", shell_pod: str = "artifact-shell") -> int:
        self.ensure_artifact_shell(pvc=pvc, shell_pod=shell_pod)
        return self.runner.interactive(self._ns(["exec", "-it", shell_pod, "--", "sh"]))

    def set_cronjob_suspend(self, component: str, suspend: bool) -> CronJobStatus:
        cronjob = cronjob_for_component(component)
        payload = json.dumps({"spec": {"suspend": suspend}})
        self._run(["patch", "cronjob", cronjob, "--type", "merge", "-p", payload])
        item = self._run_json(["get", "cronjob", cronjob, "-o", "json"])
        return _cronjob_status(item, None)

    def config_get(self, path: str) -> ConfigValueReport:
        _validate_config_setting_path(path)
        config = self._cluster_config()
        value, exists = _get_config_setting(config, path)
        return ConfigValueReport(path=path, value=value, exists=exists)

    def config_set(self, path: str, raw_value: str) -> ConfigSetReport:
        _validate_config_setting_path(path)
        value = _parse_config_setting_value(path, raw_value)
        configmap = self._run_json(["get", "configmap", "agentlab-config", "-o", "json"])
        config = _config_from_configmap_document(configmap)
        before, before_exists = _get_config_setting(config, path)
        _set_config_setting(config, path, value)
        patch = {"data": {"config.yaml": yaml.safe_dump(config, sort_keys=False)}}
        self._run(["patch", "configmap", "agentlab-config", "--type", "merge", "-p", json.dumps(patch)])
        after, _ = _get_config_setting(config, path)
        return ConfigSetReport(
            path=path,
            before=before,
            after=after,
            before_exists=before_exists,
            changed=not before_exists or before != after,
        )

    def mrs(
        self,
        *,
        state: str = "opened",
        label: str = "agent/generated",
        secret_name: str = "agentlab-secrets",
    ) -> MergeRequestListReport:
        configmap = self._run_json(["get", "configmap", "agentlab-config", "-o", "json"])
        config = AppConfig.model_validate(_config_from_configmap_document(configmap))
        secret = self._run_json(["get", "secret", secret_name, "-o", "json"])
        token = _secret_value(secret, config.gitlab_token_env)
        try:
            tool = self._new_gitlab_tool(config, token)
            if hasattr(tool, "list_agent_merge_requests"):
                raw_mrs = tool.list_agent_merge_requests(state=state, label=label)
            else:
                raw_mrs = tool.list_open_agent_mrs()
        except Exception as exc:
            message = _safe_error(exc)
            if token:
                message = message.replace(token, "REDACTED")
            raise K8sOperatorError(f"could not list GitLab merge requests: {message}") from exc
        mrs = _agent_mr_details(raw_mrs, state=state, label=label, default_branch=config.default_branch)
        return MergeRequestListReport(namespace=self.namespace, state=state, label=label, merge_requests=mrs)

    def health(
        self,
        *,
        manifest_dir: str | Path | None = DEFAULT_MANIFEST_DIR,
        pvc: str = "agentlab-runs",
        shell_pod: str = "artifact-shell",
    ) -> HealthReport:
        status = self.status(manifest_dir=manifest_dir)
        config = self._cluster_config()
        scheduler_state, scheduler_state_meta = self._scheduler_state(pvc=pvc, shell_pod=shell_pod)
        doctor = self._doctor_health(status)
        images = _health_images(status)
        failed_resources = {
            "jobs": [job.__dict__ for job in status.failed_jobs],
            "pods": [pod.__dict__ for pod in status.failed_pods],
        }
        scheduler = _health_scheduler(config, scheduler_state, scheduler_state_meta)
        models = _health_models(config, doctor)
        open_mr_count = len(status.open_agent_mrs)
        if status.open_agent_mrs_warning and scheduler.get("open_agent_mrs_count") is not None:
            open_mr_count = scheduler["open_agent_mrs_count"]
        gitlab = {
            "url": config.get("gitlab_url"),
            "project_id": config.get("project_id"),
            "open_agent_mrs_count": open_mr_count,
            "status": "warning" if status.open_agent_mrs_warning else "ok",
            "warning": status.open_agent_mrs_warning,
        }
        warnings = _health_warnings(status, scheduler_state_meta, doctor, images, failed_resources)
        return HealthReport(
            namespace=self.namespace,
            status=_health_overall_status(warnings, failed_resources, doctor),
            images=images,
            failed_resources=failed_resources,
            open_agent_mrs=status.open_agent_mrs,
            gitlab=gitlab,
            scheduler=scheduler,
            models=models,
            doctor=doctor,
            warnings=warnings,
        )

    def _new_gitlab_tool(self, config: AppConfig, token: str) -> Any:
        try:
            return self.gitlab_tool_factory(config, token=token)
        except TypeError as exc:
            try:
                return self.gitlab_tool_factory(config)
            except TypeError:
                raise exc

    def failed_resources(self) -> FailedResources:
        jobs: list[str] = []
        pods: list[str] = []
        skipped: list[str] = []
        for item in self._items("jobs"):
            name = _resource_name(item)
            if not name:
                continue
            if not name.startswith("agentlab-"):
                if _job_failed(item):
                    skipped.append(f"job/{name}: not an AgentLab resource")
                continue
            if _job_active(item):
                skipped.append(f"job/{name}: still active")
                continue
            if _job_failed(item):
                jobs.append(name)

        for item in self._items("pods"):
            name = _resource_name(item)
            if not name:
                continue
            if not name.startswith("agentlab-"):
                if _pod_failed(item):
                    skipped.append(f"pod/{name}: not an AgentLab resource")
                continue
            if _pod_running(item):
                skipped.append(f"pod/{name}: still running")
                continue
            if _pod_failed(item):
                pods.append(name)
        return FailedResources(jobs=sorted(jobs), pods=sorted(pods), skipped_resources=skipped)

    def cleanup_failed(self, *, dry_run: bool = False) -> CleanupReport:
        resources = self.failed_resources()
        deleted_jobs: list[str] = []
        deleted_pods: list[str] = []
        if not dry_run:
            for job in resources.jobs:
                self._run(["delete", "job", job, "--ignore-not-found=true"])
                deleted_jobs.append(job)
            for pod in resources.pods:
                self._run(["delete", "pod", pod, "--ignore-not-found=true"])
                deleted_pods.append(pod)
        return CleanupReport(
            namespace=self.namespace,
            deleted_jobs=resources.jobs if dry_run else deleted_jobs,
            deleted_pods=resources.pods if dry_run else deleted_pods,
            skipped_resources=resources.skipped_resources,
            dry_run=dry_run,
        )

    def upgrade(
        self,
        *,
        image: str,
        version: str | None = None,
        apply: bool = False,
        preserve_cluster_config: bool = False,
        preserve_local_config: bool = False,
        run_doctor: bool = False,
        show_status: bool = False,
        cleanup_failed: bool = False,
    ) -> UpgradeReport:
        if preserve_cluster_config and preserve_local_config:
            raise K8sOperatorError("Choose either --preserve-cluster-config or --preserve-local-config, not both.")
        if not self.manifest_dir.exists():
            raise K8sOperatorError(f"manifest dir is missing: {self.manifest_dir}")
        if not self.manifest_dir.is_dir():
            raise K8sOperatorError(f"manifest dir is not a directory: {self.manifest_dir}")

        preserved_config: dict[str, Any] | None = None
        if preserve_cluster_config:
            preserved_config = self._cluster_config()
        elif preserve_local_config:
            preserved_config = _config_from_configmap_manifest(self.manifest_dir / "configmap.yaml")

        updated = update_generated_manifests(
            manifest_dir=self.manifest_dir,
            image=image,
            version=version,
            preserved_config=preserved_config,
        )
        expected_image, drifts = detect_manifest_image_drift(self.manifest_dir)
        drift_messages = [
            f"{drift.path}: {drift.image} != {drift.expected_image}"
            for drift in drifts
        ]
        if expected_image != image:
            drift_messages.append(f"configmap.yaml annotation: {expected_image or 'missing'} != {image}")

        report = UpgradeReport(
            namespace=self.namespace,
            manifest_dir=str(self.manifest_dir),
            image=image,
            version=version,
            updated_manifests=updated["updated_manifests"],
            preserved_sections=updated["preserved_sections"],
            apply=apply,
            run_doctor=run_doctor,
            cleanup_failed=cleanup_failed,
            image_drift=drift_messages,
        )
        if report.image_drift:
            return report

        if apply:
            self._run(["apply", "-k", str(self.manifest_dir)])
            for manifest in updated["enabled_cronjob_manifests"]:
                path = self.manifest_dir / manifest
                if not path.exists():
                    raise K8sOperatorError(f"enabled CronJob manifest is missing after upgrade: {path}")
                self._run(["apply", "-f", str(path)])
            report.applied = True
            if show_status or apply:
                report.status_checked = True
                cluster_status = self.status(manifest_dir=self.manifest_dir)
                cluster_drifts = [
                    f"CronJob {item.name}: {item.image} != {cluster_status.configmap_image}"
                    for item in cluster_status.cronjobs
                    if item.image_drift
                ]
                if cluster_drifts:
                    report.image_drift.extend(cluster_drifts)
                    raise K8sOperatorError("image drift remains after apply: " + "; ".join(cluster_drifts))
            if run_doctor:
                report.doctor_status = self.run_doctor_job()
            if cleanup_failed:
                report.cleanup_report = self.cleanup_failed(dry_run=False)
        return report

    def _cluster_config(self) -> dict[str, Any]:
        configmap = self._run_json(["get", "configmap", "agentlab-config", "-o", "json"])
        config_text = ((configmap.get("data") or {}).get("config.yaml") or "")
        if not config_text:
            return {}
        loaded = yaml.safe_load(config_text) or {}
        if not isinstance(loaded, dict):
            return {}
        return loaded

    def _open_agent_mrs_from_configmap(self, configmap: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        if not configmap:
            return [], "could not read open Agent MRs: agentlab-config ConfigMap is unavailable"
        if not ((configmap.get("data") or {}).get("config.yaml")):
            return [], "could not read open Agent MRs: config.yaml is unavailable in agentlab-config"
        config: AppConfig | None = None
        try:
            config = AppConfig.model_validate(_config_from_configmap_document(configmap))
            mrs = self.gitlab_tool_factory(config).list_open_agent_mrs()
            return _open_agent_mr_details(mrs), None
        except Exception as exc:
            return [], _open_agent_mrs_warning(config, exc)

    def _scheduler_state(self, *, pvc: str, shell_pod: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = f"{RUNS_ROOT}/scheduler/state.json"
        meta: dict[str, Any] = {"path": path, "exists": False, "warning": None}
        try:
            self.ensure_artifact_shell(pvc=pvc, shell_pod=shell_pod)
            exists = self._exec(shell_pod, f"test -f {_sh_quote(path)}", check=False)
            if exists.returncode != 0:
                meta["warning"] = "scheduler state file not found"
                return {}, meta
            raw_mtime = self._exec(shell_pod, f"stat -c %Y {_sh_quote(path)}", check=False)
            if raw_mtime.returncode == 0:
                try:
                    mtime_epoch = float(raw_mtime.stdout.strip())
                    meta["mtime_epoch"] = mtime_epoch
                    meta["age_seconds"] = int(max(0, time.time() - mtime_epoch))
                except ValueError:
                    meta["warning"] = "scheduler state mtime is not parseable"
            raw_state = self._exec(shell_pod, f"cat {_sh_quote(path)}", check=False)
            if raw_state.returncode != 0:
                meta["warning"] = _safe_message(raw_state.stderr or raw_state.stdout or "could not read scheduler state")
                return {}, meta
            loaded = json.loads(raw_state.stdout or "{}")
            if not isinstance(loaded, dict):
                meta["warning"] = "scheduler state is not a JSON object"
                return {}, meta
            meta["exists"] = True
            return loaded, meta
        except Exception as exc:
            meta["warning"] = _safe_error(exc)
            return {}, meta

    def _doctor_health(self, status: ClusterStatus) -> dict[str, Any]:
        doctor_jobs = [job for job in status.recent_jobs if job.name.startswith(JOB_PREFIXES["doctor"])]
        if not doctor_jobs:
            return {"status": "unknown", "job": None, "job_status": "missing", "warning": "no doctor job found"}
        job = doctor_jobs[0]
        result = self.runner.run(self._ns(["logs", f"job/{job.name}", "--tail=200"]), check=False)
        if result.returncode != 0:
            return {
                "status": "unknown",
                "job": job.name,
                "job_status": job.status,
                "warning": _safe_message(result.stderr or result.stdout or "could not read doctor logs"),
            }
        logs = result.stdout or ""
        if "AgentLab doctor: failed" in logs:
            doctor_status = "failed"
        elif "AgentLab doctor: warning" in logs:
            doctor_status = "warning"
        elif "AgentLab doctor: passed" in logs:
            doctor_status = "passed"
        else:
            doctor_status = "unknown"
        return {
            "status": doctor_status,
            "job": job.name,
            "job_status": job.status,
            "models": _doctor_model_signals(logs),
            "warning": None if doctor_status != "unknown" else "doctor logs did not contain a status line",
        }

    def _run_artifact_status(self, run_id: str, *, shell_pod: str) -> tuple[str, str]:
        for artifact in (
            "scheduler_report.json",
            "review_comment_report.json",
            "gate_decision.json",
            "manifest.json",
        ):
            path = artifact_path(run_id, artifact)
            if self._exec(shell_pod, f"test -f {_sh_quote(path)}", check=False).returncode != 0:
                continue
            raw = self._exec(shell_pod, f"cat {_sh_quote(path)}", check=False).stdout
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            status = str(payload.get("status") or payload.get("verdict") or payload.get("state") or "unknown")
            reason = _reason_from_payload(payload)
            return status, reason
        return "unknown", ""

    def _items(self, resource: str) -> list[dict[str, Any]]:
        payload = self._run_json(["get", resource, "-o", "json"], check=False)
        return payload.get("items", []) if isinstance(payload, dict) else []

    def _run_json(self, args: list[str], *, check: bool = True) -> dict[str, Any]:
        result = self.runner.run(self._ns(args), check=check)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        return json.loads(result.stdout)

    def _run(self, args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
        return self.runner.run(self._ns(args), input_text=input_text, check=check).stdout

    def _logs_with_retry(self, args: list[str]) -> str:
        attempts = max(1, self.log_retry_attempts)
        last_result: KubectlResult | None = None
        for attempt in range(attempts):
            result = self.runner.run(self._ns(args), check=False)
            if result.returncode == 0:
                return result.stdout
            last_result = result
            message = f"{result.stderr}\n{result.stdout}"
            if not _is_transient_log_error(message) or attempt == attempts - 1:
                break
            if self.log_retry_delay_seconds > 0:
                time.sleep(self.log_retry_delay_seconds)
        message = ""
        if last_result is not None:
            message = (last_result.stderr or last_result.stdout).strip()
        raise K8sOperatorError(message or f"kubectl logs failed: {' '.join(args)}")

    def _stream_with_retry(self, args: list[str]) -> str:
        attempts = max(1, self.log_retry_attempts)
        stream_args = [*args, "-f"]
        last_result: KubectlResult | None = None
        for attempt in range(attempts):
            result = self._stream(stream_args)
            if result.returncode == 0:
                return ""
            last_result = result
            message = f"{result.stderr}\n{result.stdout}"
            if not _is_transient_log_error(message) or attempt == attempts - 1:
                break
            if self.log_retry_delay_seconds > 0:
                time.sleep(self.log_retry_delay_seconds)
        message = ""
        if last_result is not None:
            message = (last_result.stderr or last_result.stdout).strip()
        if message:
            raise K8sOperatorError(message)
        code = last_result.returncode if last_result is not None else "unknown"
        raise K8sOperatorError(f"kubectl logs failed with exit code {code}")

    def _doctor_logs_with_status_retry(self, job_name: str) -> str:
        attempts = max(1, self.log_retry_attempts)
        last_logs = ""
        for attempt in range(attempts):
            logs = self.job_logs(job_name, follow=False)
            if _doctor_logs_have_status(logs):
                return logs
            last_logs = logs
            if attempt < attempts - 1 and self.log_retry_delay_seconds > 0:
                time.sleep(self.log_retry_delay_seconds)
        if last_logs.strip():
            raise K8sOperatorError(
                "Doctor logs did not contain an AgentLab doctor status line after retries. "
                f"Last log snippet: {_log_snippet(last_logs)}"
            )
        raise K8sOperatorError("Doctor logs were empty after retries.")

    def _stream(self, args: list[str]) -> KubectlResult:
        namespaced_args = self._ns(args)
        result = self.runner.stream(namespaced_args)
        if isinstance(result, int):
            return KubectlResult(args=namespaced_args, returncode=result)
        return result

    def _exec(self, shell_pod: str, command: str, *, check: bool = True) -> KubectlResult:
        return self.runner.run(self._ns(["exec", shell_pod, "--", "sh", "-c", command]), check=check)

    def _ns(self, args: list[str]) -> list[str]:
        return ["-n", self.namespace, *args]


def job_prefix_for_component(component: str) -> str:
    try:
        return JOB_PREFIXES[component]
    except KeyError as exc:
        raise K8sOperatorError(f"unknown logs component: {component}") from exc


def run_job_name_for_component(component: str) -> str:
    try:
        return RUN_JOB_NAMES[component]
    except KeyError as exc:
        raise K8sOperatorError(f"unknown run component: {component}") from exc


def manifest_for_component(component: str, manifest_dir: str | Path = DEFAULT_MANIFEST_DIR) -> Path:
    try:
        return Path(manifest_dir) / RUN_MANIFESTS[component]
    except KeyError as exc:
        raise K8sOperatorError(f"unknown run component: {component}") from exc


def cronjob_for_component(component: str) -> str:
    try:
        return CRONJOBS[component]
    except KeyError as exc:
        raise K8sOperatorError(f"unknown CronJob component: {component}") from exc


def kubectl_args(namespace: str, args: list[str]) -> list[str]:
    return ["-n", namespace, *args]


def artifact_path(run_id: str, artifact: str) -> str:
    return f"{RUNS_ROOT}/{_safe_name(run_id, 'run_id')}/artifacts/{_safe_artifact_path(artifact)}"


def render_artifact_shell_pod(namespace: str, pvc: str, shell_pod: str) -> str:
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {shell_pod}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: agentlab
    app.kubernetes.io/component: artifact-shell
spec:
  restartPolicy: Always
  containers:
    - name: shell
      image: {ARTIFACT_SHELL_IMAGE}
      command: ["sh", "-c", "sleep 365d"]
      stdin: true
      tty: true
      volumeMounts:
        - name: runs
          mountPath: /var/lib/agentlab
  volumes:
    - name: runs
      persistentVolumeClaim:
        claimName: {pvc}
"""


def update_generated_manifests(
    *,
    manifest_dir: Path,
    image: str,
    version: str | None = None,
    preserved_config: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    configmap_path = manifest_dir / "configmap.yaml"
    if not configmap_path.exists():
        raise K8sOperatorError(f"missing manifest: {configmap_path}")

    updated_manifests: list[str] = []
    preserved_sections: list[str] = []
    configmap = _load_yaml_file(configmap_path)
    metadata = configmap.setdefault("metadata", {})
    annotations = metadata.setdefault("annotations", {})
    annotations.pop(DEPRECATED_K8S_IMAGE_ANNOTATION, None)
    annotations[K8S_IMAGE_ANNOTATION] = image
    if version:
        annotations[K8S_VERSION_ANNOTATION] = version
    config = _config_from_configmap_document(configmap)
    if preserved_config:
        preserved_sections = _merge_preserved_config_sections(config, preserved_config)
    config["auto_merge_enabled"] = False
    config["direct_main_push_enabled"] = False
    enabled_cronjob_manifests = _enabled_cronjob_manifest_names(config)
    for manifest in _ensure_enabled_cronjob_manifests(manifest_dir, config):
        if manifest not in updated_manifests:
            updated_manifests.append(manifest)
    data = configmap.setdefault("data", {})
    data["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
    _write_yaml_file(configmap_path, configmap)
    updated_manifests.append(configmap_path.name)

    for path in sorted([*manifest_dir.glob("job-*.yaml"), *manifest_dir.glob("cronjob-*.yaml")]):
        document = _load_yaml_file(path)
        changed = _set_manifest_image(document, image)
        changed = _ensure_generated_job_safeguards(document) or changed
        if changed:
            _write_yaml_file(path, document)
            if path.name not in updated_manifests:
                updated_manifests.append(path.name)

    if _ensure_kustomization_includes_cronjobs(manifest_dir, enabled_cronjob_manifests):
        updated_manifests.append("kustomization.yaml")
    return {
        "updated_manifests": updated_manifests,
        "preserved_sections": preserved_sections,
        "enabled_cronjob_manifests": enabled_cronjob_manifests,
    }


def _ensure_generated_job_safeguards(document: dict[str, Any]) -> bool:
    kind = document.get("kind")
    if kind == "Job":
        changed = _ensure_job_spec_safeguards(document.setdefault("spec", {}))
        changed = _ensure_container_resources(_job_container_specs(document)) or changed
        changed = _ensure_container_path_env(_job_container_specs(document)) or changed
        return changed
    if kind == "CronJob":
        changed = False
        spec = document.setdefault("spec", {})
        if spec.get("concurrencyPolicy") != "Forbid":
            spec["concurrencyPolicy"] = "Forbid"
            changed = True
        job_spec = spec.setdefault("jobTemplate", {}).setdefault("spec", {})
        changed = _ensure_job_spec_safeguards(job_spec, ttl=False) or changed
        changed = _ensure_container_resources(_cronjob_container_specs(document)) or changed
        changed = _ensure_container_path_env(_cronjob_container_specs(document)) or changed
        return changed
    return False


def _ensure_job_spec_safeguards(spec: dict[str, Any], *, ttl: bool = True) -> bool:
    changed = False
    if "backoffLimit" not in spec:
        spec["backoffLimit"] = DEFAULT_JOB_BACKOFF_LIMIT
        changed = True
    if "activeDeadlineSeconds" not in spec:
        spec["activeDeadlineSeconds"] = DEFAULT_JOB_ACTIVE_DEADLINE_SECONDS
        changed = True
    if ttl and "ttlSecondsAfterFinished" not in spec:
        spec["ttlSecondsAfterFinished"] = DEFAULT_JOB_TTL_SECONDS_AFTER_FINISHED
        changed = True
    return changed


def _ensure_container_resources(containers: list[dict[str, Any]]) -> bool:
    changed = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        resources = container.get("resources")
        if not isinstance(resources, dict):
            resources = {}
            container["resources"] = resources
            changed = True
        for section, defaults in DEFAULT_JOB_RESOURCES.items():
            values = resources.get(section)
            if not isinstance(values, dict):
                values = {}
                resources[section] = values
                changed = True
            for key, value in defaults.items():
                if key not in values:
                    values[key] = value
                    changed = True
    return changed


def _ensure_container_path_env(containers: list[dict[str, Any]]) -> bool:
    changed = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        env = container.get("env")
        if not isinstance(env, list):
            env = []
            container["env"] = env
            changed = True
        path_env = next((item for item in env if isinstance(item, dict) and item.get("name") == "PATH"), None)
        if path_env is None:
            env.append({"name": "PATH", "value": RUNTIME_PATH})
            changed = True
        elif path_env.get("value") != RUNTIME_PATH:
            path_env["value"] = RUNTIME_PATH
            changed = True
    return changed


def _job_container_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    containers = (((document.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers")
    return containers if isinstance(containers, list) else []


def _cronjob_container_specs(document: dict[str, Any]) -> list[dict[str, Any]]:
    containers = (
        ((((document.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}).get("template") or {})
        .get("spec", {})
        .get("containers")
    )
    return containers if isinstance(containers, list) else []


def detect_manifest_image_drift(manifest_dir: Path) -> tuple[str | None, list[ManifestImageDrift]]:
    configmap_path = manifest_dir / "configmap.yaml"
    expected_image: str | None = None
    if configmap_path.exists():
        configmap = yaml.safe_load(configmap_path.read_text(encoding="utf-8")) or {}
        expected_image = _configmap_image(configmap)
    if not expected_image:
        return expected_image, []

    drifts: list[ManifestImageDrift] = []
    for path in sorted(manifest_dir.glob("*.yaml")):
        if path.name == "configmap.yaml":
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        image = _manifest_image(document)
        if image and image != expected_image:
            drifts.append(ManifestImageDrift(path=path.name, image=image, expected_image=expected_image))
    return expected_image, drifts


def format_status(status: ClusterStatus) -> str:
    lines = [
        f"Namespace: {status.namespace}",
        f"ConfigMap image: {status.configmap_image or 'not found'}",
        f"ConfigMap version: {status.configmap_version or 'not found'}",
        "",
        "CronJobs:",
    ]
    if status.cronjobs:
        for item in status.cronjobs:
            drift = " [image drift]" if item.image_drift else ""
            lines.append(
                f"- {item.name}: schedule={item.schedule}, suspend={str(item.suspend).lower()}, "
                f"active={item.active}, last={item.last_schedule}, image={item.image}{drift}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Open Agent MRs:"])
    if status.open_agent_mrs:
        for mr in status.open_agent_mrs:
            iid = mr.get("iid")
            title = str(mr.get("title") or "<untitled>")
            branch = str(mr.get("source_branch") or "unknown")
            url = str(mr.get("web_url") or "no-url")
            prefix = f"!{iid}" if iid is not None else "!?"
            lines.append(f"- {prefix} [agent] {title} | branch={branch} | {url}")
    elif status.open_agent_mrs_warning:
        lines.append("- unknown")
    else:
        lines.append("- none")

    lines.extend(["", "Recent jobs:"])
    lines.extend(f"- {job.name}: {job.status} ({job.created_at or 'unknown'})" for job in status.recent_jobs[:10] or [])
    if not status.recent_jobs:
        lines.append("- none")

    lines.extend(["", "Failed jobs/pods:"])
    failed_lines = [f"- job/{job.name}: {job.status}" for job in status.failed_jobs]
    failed_lines.extend(f"- pod/{pod.name}: {pod.phase} {pod.reason}".rstrip() for pod in status.failed_pods)
    lines.extend(failed_lines or ["- none"])

    warnings = []
    warnings.extend(
        f"CronJob {item.name} image {item.image} differs from ConfigMap annotation {status.configmap_image}"
        for item in status.cronjobs
        if item.image_drift
    )
    warnings.extend(
        f"Manifest {drift.path} image {drift.image} differs from generated ConfigMap annotation {drift.expected_image}"
        for drift in status.manifest_image_drifts
    )
    if status.open_agent_mrs_warning:
        warnings.append(status.open_agent_mrs_warning)
        export_command = _gitlab_token_export_command(status.open_agent_mrs_warning, status.namespace)
        if export_command:
            warnings.append(f"Load it from Kubernetes with: {export_command}")
    if status.image_annotation_warning:
        warnings.append(status.image_annotation_warning)
    if status.manifest_image_annotation_warning:
        warnings.append(f"Generated manifest: {status.manifest_image_annotation_warning}")
    if status.manifest_configmap_image:
        lines.extend(["", f"Generated manifest ConfigMap image: {status.manifest_configmap_image}"])
    if status.manifest_configmap_version:
        lines.append(f"Generated manifest ConfigMap version: {status.manifest_configmap_version}")
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def format_mrs(report: MergeRequestListReport) -> str:
    if not report.merge_requests:
        return "No AgentLab merge requests found."
    lines = []
    for mr in report.merge_requests:
        iid = mr.get("iid")
        prefix = f"!{iid}" if iid is not None else "!?"
        labels = ", ".join(str(label) for label in mr.get("labels", []))
        lines.append(
            " | ".join(
                [
                    prefix,
                    str(mr.get("title") or ""),
                    str(mr.get("state") or ""),
                    str(mr.get("source_branch") or ""),
                    str(mr.get("web_url") or ""),
                    labels,
                ]
            )
        )
    return "\n".join(lines)


def format_health(report: HealthReport) -> str:
    review = report.scheduler.get("review_comments") or {}
    failed_jobs = report.failed_resources.get("jobs") or []
    failed_pods = report.failed_resources.get("pods") or []
    drift = report.images.get("drift") or []
    model_names = report.models.get("models") or {}
    author_config = (
        f"authors={_display_list(review.get('allowed_authors'))}, "
        f"roles={_display_list(review.get('require_author_role'))}"
    )
    lines = [
        f"AgentLab health: {report.status}",
        f"Namespace: {report.namespace}",
        "",
        "Runtime:",
        f"- ConfigMap image: {_display_value(report.images.get('configmap_image'))}",
        f"- ConfigMap version: {_display_value(report.images.get('configmap_version'))}",
        f"- Generated manifest image: {_display_value(report.images.get('generated_configmap_image'))}",
        f"- Generated manifest version: {_display_value(report.images.get('generated_configmap_version'))}",
        f"- Image drift: {'none' if not drift else str(len(drift))}",
        f"- Failed jobs: {len(failed_jobs)}",
        f"- Failed pods: {len(failed_pods)}",
        "",
        "Scheduler:",
        f"- action: {_enabled_label(report.scheduler.get('action_enabled'))}",
        f"- review-comments: {_enabled_label(review.get('enabled'))} ({author_config})",
        f"- scheduler state age: {_format_age(report.scheduler.get('state_age_seconds'))}",
        f"- last watch: {_display_value(report.scheduler.get('last_watch_run'))}",
        f"- last plan: {_display_value(report.scheduler.get('last_plan_run'))}",
        f"- last action: {_display_value(report.scheduler.get('last_action_run'))}",
        f"- last review: {_display_value(report.scheduler.get('last_review_run'))}",
        "",
        "GitLab:",
        f"- project: {_display_value(report.gitlab.get('url'))} / {_display_value(report.gitlab.get('project_id'))}",
        f"- Open Agent MRs: {_display_value(report.gitlab.get('open_agent_mrs_count'))}",
    ]
    if report.open_agent_mrs:
        for mr in report.open_agent_mrs:
            iid = mr.get("iid")
            prefix = f"!{iid}" if iid is not None else "!?"
            title = str(mr.get("title") or "<untitled>")
            branch = str(mr.get("source_branch") or "unknown")
            url = str(mr.get("web_url") or "no-url")
            lines.append(f"- {prefix} {title} | {branch} | {url}")

    lines.extend(
        [
            "",
            "Models:",
            f"- base URL: {_display_value(report.models.get('base_url'))}",
            f"- default: {_display_value(report.models.get('default'))}",
            f"- configured: {_display_list(model_names.keys() if isinstance(model_names, dict) else [])}",
            "",
            "Doctor:",
            f"- status: {_display_value(report.doctor.get('status'))}",
            f"- job: {_display_value(report.doctor.get('job'))}",
        ]
    )

    if drift:
        lines.extend(["", "Image Drift:"])
        lines.extend(f"- {_display_value(item)}" for item in drift)
    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {_display_value(warning)}" for warning in report.warnings)
    return "\n".join(lines)


def format_runs(runs: list[RunSummary]) -> str:
    if not runs:
        return "No run directories found."
    return "\n".join(f"- {run.run_id}: {run.mtime} | {run.status} | {run.reason}".rstrip() for run in runs)


def format_failed_resources(resources: FailedResources, *, namespace: str) -> str:
    if not resources.found:
        return "No failed AgentLab resources found."
    lines = [
        f"Found failed AgentLab resources in namespace {namespace}:",
        "",
        "Jobs:",
    ]
    lines.extend(f"- {name}" for name in resources.jobs)
    if not resources.jobs:
        lines.append("- none")
    lines.extend(["", "Pods:"])
    lines.extend(f"- {name}" for name in resources.pods)
    if not resources.pods:
        lines.append("- none")
    if resources.skipped_resources:
        lines.extend(["", "Skipped resources:"])
        lines.extend(f"- {item}" for item in resources.skipped_resources)
    return "\n".join(lines)


def format_cleanup_report(report: CleanupReport) -> str:
    if report.dry_run:
        header = f"Dry run: no resources deleted in namespace {report.namespace}."
    else:
        header = f"Cleanup summary for namespace {report.namespace}:"
    lines = [header, "", "Deleted jobs:"]
    lines.extend(f"- job/{name}" for name in report.deleted_jobs)
    if not report.deleted_jobs:
        lines.append("- none")
    lines.extend(["", "Deleted pods:"])
    lines.extend(f"- pod/{name}" for name in report.deleted_pods)
    if not report.deleted_pods:
        lines.append("- none")
    lines.extend(["", "Skipped resources:"])
    lines.extend(f"- {item}" for item in report.skipped_resources)
    if not report.skipped_resources:
        lines.append("- none")
    return "\n".join(lines)


def format_upgrade_report(report: UpgradeReport) -> str:
    lines = [
        "AgentLab Kubernetes upgrade plan",
        "",
        f"Namespace: {report.namespace}",
        f"Manifest dir: {report.manifest_dir}",
        f"New image: {report.image}",
        f"New version: {getattr(report, 'version', None) or 'not set'}",
        "",
        "Updated manifests:",
    ]
    lines.extend(f"- {name}" for name in report.updated_manifests)
    if not report.updated_manifests:
        lines.append("- none")
    lines.extend(["", "Preserved config sections:"])
    lines.extend(f"- {section}" for section in report.preserved_sections)
    if not report.preserved_sections:
        lines.append("- none")
    lines.extend(
        [
            "",
            f"Apply: {'yes' if report.apply else 'no'}",
            f"Doctor: {'yes' if report.run_doctor else 'no'}",
            f"Cleanup failed: {'yes' if report.cleanup_failed else 'no'}",
            "",
            "Result:",
            f"- {'applied' if report.applied else 'generated only'}",
            f"- image drift: {'none' if not report.image_drift else '; '.join(report.image_drift)}",
            f"- doctor: {report.doctor_status}",
        ]
    )
    if report.apply and not report.applied and report.image_drift:
        lines.append("- Upgrade was not applied because manifest drift/preflight failed.")
    if report.cleanup_report is not None:
        lines.append(f"- cleanup deleted jobs: {len(report.cleanup_report.deleted_jobs)}")
        lines.append(f"- cleanup deleted pods: {len(report.cleanup_report.deleted_pods)}")
    return "\n".join(lines)


def _health_images(status: ClusterStatus) -> dict[str, Any]:
    drift = [
        f"CronJob {item.name}: {item.image} != {status.configmap_image}"
        for item in status.cronjobs
        if item.image_drift
    ]
    drift.extend(
        f"Manifest {item.path}: {item.image} != {item.expected_image}"
        for item in status.manifest_image_drifts
    )
    return {
        "configmap_image": status.configmap_image,
        "configmap_version": status.configmap_version,
        "generated_configmap_image": status.manifest_configmap_image,
        "generated_configmap_version": status.manifest_configmap_version,
        "cronjobs": [
            {
                "name": item.name,
                "schedule": item.schedule,
                "suspend": item.suspend,
                "active": item.active,
                "last_schedule": item.last_schedule,
                "image": item.image,
                "image_drift": item.image_drift,
            }
            for item in status.cronjobs
        ],
        "drift": drift,
    }


def _health_scheduler(
    config: dict[str, Any],
    state: dict[str, Any],
    state_meta: dict[str, Any],
) -> dict[str, Any]:
    schedule = _as_mapping(config.get("schedule"))
    action = _as_mapping(schedule.get("action"))
    review = _as_mapping(schedule.get("review_comments"))
    return {
        "state_path": state_meta.get("path"),
        "state_exists": bool(state_meta.get("exists")),
        "state_age_seconds": state_meta.get("age_seconds"),
        "state_warning": state_meta.get("warning"),
        "schedule_enabled": _optional_bool(schedule.get("enabled")),
        "last_watch_run": state.get("last_watch_run"),
        "last_plan_run": state.get("last_plan_run"),
        "last_action_run": state.get("last_action_run"),
        "last_review_run": state.get("last_review_comment_run"),
        "open_agent_mrs_count": state.get("open_agent_mrs"),
        "last_selected_task_id": state.get("last_selected_task_id"),
        "closed_agent_mr_feedback_count": _collection_size(state.get("closed_agent_mr_feedback")),
        "processed_review_comments_count": _collection_size(state.get("processed_review_comments")),
        "action_enabled": _optional_bool(action.get("enabled")),
        "action_cron": action.get("cron"),
        "review_comments": {
            "enabled": _optional_bool(review.get("enabled")),
            "cron": review.get("cron"),
            "allowed_authors": _string_list(review.get("allowed_authors")),
            "require_author_role": _string_list(review.get("require_author_role")),
            "process_history": _optional_bool(review.get("process_history")),
            "cooldown_minutes": review.get("cooldown_minutes"),
            "max_comments_per_run": review.get("max_comments_per_run"),
        },
    }


def _health_models(config: dict[str, Any], doctor: dict[str, Any]) -> dict[str, Any]:
    ollama = _as_mapping(config.get("ollama"))
    models = _as_mapping(ollama.get("models"))
    return {
        "base_url": ollama.get("base_url") or "http://localhost:11434",
        "default": models.get("default") or "qwen3.6:35b",
        "models": models,
        "doctor_status": doctor.get("status"),
        "doctor_signals": doctor.get("models") or [],
    }


def _health_warnings(
    status: ClusterStatus,
    state_meta: dict[str, Any],
    doctor: dict[str, Any],
    images: dict[str, Any],
    failed_resources: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if status.image_annotation_warning:
        warnings.append(status.image_annotation_warning)
    if status.manifest_image_annotation_warning:
        warnings.append(f"Generated manifest: {status.manifest_image_annotation_warning}")
    if status.open_agent_mrs_warning:
        warnings.append(status.open_agent_mrs_warning)
    if state_meta.get("warning"):
        warnings.append(f"scheduler state: {state_meta['warning']}")
    warnings.extend(f"image drift: {item}" for item in images.get("drift") or [])
    failed_jobs = failed_resources.get("jobs") or []
    failed_pods = failed_resources.get("pods") or []
    if failed_jobs:
        warnings.append(f"{len(failed_jobs)} failed AgentLab job(s)")
    if failed_pods:
        warnings.append(f"{len(failed_pods)} failed AgentLab pod(s)")
    if doctor.get("status") in {"failed", "warning"}:
        warnings.append(f"doctor status: {doctor.get('status')}")
    if doctor.get("warning"):
        warnings.append(f"doctor: {doctor['warning']}")
    return _dedupe_strings(_safe_message(warning) for warning in warnings)


def _health_overall_status(
    warnings: list[str],
    failed_resources: dict[str, Any],
    doctor: dict[str, Any],
) -> str:
    if failed_resources.get("jobs") or failed_resources.get("pods") or doctor.get("status") == "failed":
        return "failed"
    if warnings:
        return "warning"
    return "passed"


def _configmap_image(configmap: dict[str, Any]) -> str | None:
    metadata = configmap.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    image = annotations.get(K8S_IMAGE_ANNOTATION) or annotations.get(DEPRECATED_K8S_IMAGE_ANNOTATION)
    return str(image) if image else None


def _configmap_version(configmap: dict[str, Any]) -> str | None:
    metadata = configmap.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    version = annotations.get(K8S_VERSION_ANNOTATION)
    return str(version) if version else None


def _configmap_image_annotation_warning(configmap: dict[str, Any]) -> str | None:
    metadata = configmap.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    if DEPRECATED_K8S_IMAGE_ANNOTATION in annotations:
        return DEPRECATED_K8S_IMAGE_ANNOTATION_WARNING
    return None


def _open_agent_mr_details(mrs: list[Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for mr in mrs:
        normalized = normalize_mr(mr)
        details.append(
            {
                "iid": normalized.get("iid"),
                "title": str(normalized.get("title") or ""),
                "state": normalized.get("state"),
                "source_branch": str(normalized.get("source_branch") or ""),
                "web_url": normalized.get("web_url"),
                "labels": [str(label) for label in normalized.get("labels", [])],
                "updated_at": normalized.get("updated_at"),
            }
        )
    return details


def _agent_mr_details(
    mrs: list[Any],
    *,
    state: str,
    label: str,
    default_branch: str,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for mr in mrs:
        normalized = normalize_mr(mr)
        labels = [str(item) for item in normalized.get("labels", [])]
        source_branch = str(normalized.get("source_branch") or "")
        target_branch = str(normalized.get("target_branch") or default_branch)
        if not source_branch.startswith("agent/"):
            continue
        if target_branch != default_branch:
            continue
        if label and label not in labels:
            continue
        details.append(
            {
                "iid": normalized.get("iid"),
                "title": str(normalized.get("title") or ""),
                "state": str(normalized.get("state") or state),
                "source_branch": source_branch,
                "web_url": normalized.get("web_url"),
                "labels": labels,
                "updated_at": normalized.get("updated_at"),
            }
        )
    return details


def _secret_value(secret: dict[str, Any], key: str) -> str:
    string_data = secret.get("stringData") or {}
    if isinstance(string_data, dict) and string_data.get(key):
        return str(string_data[key])
    data = secret.get("data") or {}
    raw = data.get(key) if isinstance(data, dict) else None
    if not raw:
        raise K8sOperatorError(f"GitLab token key is missing from Kubernetes Secret: {key}")
    try:
        return base64.b64decode(str(raw)).decode("utf-8").strip()
    except Exception as exc:
        raise K8sOperatorError(f"could not decode GitLab token from Kubernetes Secret key: {key}") from exc


def _safe_error(exc: Exception) -> str:
    return str(redact_secrets(str(exc)))


def _open_agent_mrs_warning(config: AppConfig | None, exc: Exception) -> str:
    message = _safe_error(exc)
    env_var = _gitlab_token_env_from_error(message) or (config.gitlab_token_env if config is not None else None)
    if env_var and _is_missing_gitlab_token_error(message):
        return f"GitLab token env var {env_var} is not set"
    return f"could not read open Agent MRs: {message}"


def _is_missing_gitlab_token_error(message: str) -> bool:
    normalized = message.lower()
    return "gitlab token env var" in normalized and "not set" in normalized


def _gitlab_token_env_from_error(message: str) -> str | None:
    marker = "GitLab token env var is not set:"
    if marker not in message:
        return None
    remainder = message.split(marker, 1)[1].strip()
    return remainder.split()[0] if remainder else None


def _gitlab_token_env_from_warning(message: str) -> str | None:
    prefix = "GitLab token env var "
    suffix = " is not set"
    if not message.startswith(prefix) or not message.endswith(suffix):
        return None
    env_var = message[len(prefix) : -len(suffix)].strip()
    return env_var or None


def _gitlab_token_export_command(warning: str, namespace: str) -> str | None:
    env_var = _gitlab_token_env_from_warning(warning)
    if not env_var:
        return None
    return (
        f"export {env_var}=\"$(kubectl -n {namespace} get secret agentlab-secrets "
        f"-o jsonpath='{{.data.{env_var}}}' | base64 -d)\""
    )


def _safe_message(message: object) -> str:
    return str(redact_secrets(str(message))).strip()


def _as_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _collection_size(value: object) -> int:
    if isinstance(value, (list, dict, set, tuple)):
        return len(value)
    return 0


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _display_value(value: object) -> str:
    if value is None or value == "":
        return "unknown"
    return _safe_message(value)


def _display_list(values: object) -> str:
    if isinstance(values, dict):
        values = list(values)
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return "none"
    rendered = [_display_value(value) for value in values]
    return ", ".join(rendered) if rendered else "none"


def _enabled_label(value: object) -> str:
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "unknown"


def _format_age(value: object) -> str:
    if not isinstance(value, int | float):
        return "unknown"
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _doctor_model_signals(logs: str) -> list[str]:
    signals: list[str] = []
    for line in logs.splitlines():
        if "ollama" not in line.lower() and "model" not in line.lower():
            continue
        stripped = _safe_message(line)
        if stripped:
            signals.append(stripped)
    return signals[:5]


def _load_yaml_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise K8sOperatorError(f"manifest is not a YAML object: {path}")
    return data


def _write_yaml_file(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _config_from_configmap_manifest(path: Path) -> dict[str, Any]:
    return _config_from_configmap_document(_load_yaml_file(path))


def _config_from_configmap_document(configmap: dict[str, Any]) -> dict[str, Any]:
    config_text = ((configmap.get("data") or {}).get("config.yaml") or "")
    if not config_text:
        return {}
    config = yaml.safe_load(config_text) or {}
    if not isinstance(config, dict):
        return {}
    return config


def _validate_config_setting_path(path: str) -> None:
    if path not in CONFIG_SETTING_SPECS:
        allowed = ", ".join(CONFIG_SETTING_PATHS)
        raise K8sOperatorError(f"unsupported config path: {path}. Allowed paths: {allowed}")


def _parse_config_setting_value(path: str, raw_value: str) -> bool | int | str:
    value_type, min_value = CONFIG_SETTING_SPECS[path]
    value = raw_value.strip()
    if value_type is bool:
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise K8sOperatorError(f"{path} expects a boolean value: true or false")
    if value_type is int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise K8sOperatorError(f"{path} expects an integer value") from exc
        if min_value is not None and parsed < min_value:
            raise K8sOperatorError(f"{path} must be >= {min_value}")
        return parsed
    if not value:
        raise K8sOperatorError(f"{path} expects a non-empty string value")
    return value


def _get_config_setting(config: dict[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _set_config_setting(config: dict[str, Any], path: str, value: Any) -> None:
    current: dict[str, Any] = config
    parts = path.split(".")
    for index, part in enumerate(parts[:-1]):
        next_value = current.get(part)
        if next_value is None:
            next_value = {}
            current[part] = next_value
        if not isinstance(next_value, dict):
            prefix = ".".join(parts[: index + 1])
            raise K8sOperatorError(f"cannot set {path}: {prefix} is not a mapping")
        current = next_value
    current[parts[-1]] = value


def _merge_preserved_config_sections(target: dict[str, Any], source: dict[str, Any]) -> list[str]:
    preserved: list[str] = []
    for key in ("auto_approve", "required_test_commands"):
        if key in source:
            target[key] = source[key]
            preserved.append(key)
    if "schedule" in source:
        target["schedule"] = source["schedule"]
        preserved.append("schedule")
        schedule = source.get("schedule") or {}
        if isinstance(schedule, dict):
            for key in ("review_comments", "limits", "behavior"):
                if key in schedule:
                    preserved.append(f"schedule.{key}")
    return preserved


def _enabled_cronjob_manifest_names(config: dict[str, Any]) -> list[str]:
    return [CRONJOB_MANIFESTS[component] for component in _enabled_cronjob_specs(config)]


def _enabled_cronjob_specs(config: dict[str, Any]) -> dict[str, str]:
    schedule = config.get("schedule") or {}
    if not isinstance(schedule, dict):
        return {}

    specs: dict[str, str] = {}
    if bool(schedule.get("enabled", False)):
        for component in ("watch", "plan", "action"):
            entry = schedule.get(component) or {}
            if not isinstance(entry, dict):
                entry = {}
            if bool(entry.get("enabled", True)):
                specs[component] = str(entry.get("cron") or CRONJOB_DEFAULT_CRONS[component])

    review_comments = schedule.get("review_comments") or {}
    if isinstance(review_comments, dict) and bool(review_comments.get("enabled", False)):
        specs["review-comments"] = str(review_comments.get("cron") or CRONJOB_DEFAULT_CRONS["review-comments"])
    return specs


def _ensure_enabled_cronjob_manifests(manifest_dir: Path, config: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for component, cron in _enabled_cronjob_specs(config).items():
        path = manifest_dir / CRONJOB_MANIFESTS[component]
        if path.exists():
            document = _load_yaml_file(path)
            spec = document.setdefault("spec", {})
            if spec.get("schedule") != cron:
                spec["schedule"] = cron
                _write_yaml_file(path, document)
                changed.append(path.name)
            continue

        job_path = manifest_dir / RUN_MANIFESTS[component]
        if not job_path.exists():
            raise K8sOperatorError(f"missing generated Job manifest for enabled CronJob {component}: {job_path}")
        cronjob = _cronjob_from_job_manifest(component, cron, _load_yaml_file(job_path))
        _write_yaml_file(path, cronjob)
        changed.append(path.name)
    return changed


def _cronjob_from_job_manifest(component: str, cron: str, job: dict[str, Any]) -> dict[str, Any]:
    job_spec = job.get("spec") or {}
    template = job_spec.get("template")
    if not isinstance(template, dict):
        raise K8sOperatorError(f"Job manifest for enabled CronJob {component} has no pod template")

    job_metadata = job.get("metadata") or {}
    labels = dict(job_metadata.get("labels") or {})
    labels.setdefault("app.kubernetes.io/name", "agentlab")
    labels["app.kubernetes.io/component"] = "scheduler"
    metadata: dict[str, Any] = {
        "name": RUN_JOB_NAMES[component],
        "labels": labels,
    }
    if job_metadata.get("namespace"):
        metadata["namespace"] = job_metadata["namespace"]

    job_template_spec: dict[str, Any] = {"template": deepcopy(template)}
    for key in ("backoffLimit", "ttlSecondsAfterFinished", "activeDeadlineSeconds"):
        if key in job_spec:
            job_template_spec[key] = deepcopy(job_spec[key])

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": metadata,
        "spec": {
            "schedule": cron,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 5,
            "startingDeadlineSeconds": 1800,
            "jobTemplate": {"spec": job_template_spec},
        },
    }


def _set_manifest_image(document: dict[str, Any], image: str) -> bool:
    containers = _manifest_containers(document)
    if not containers:
        return False
    changed = False
    for container in containers:
        if isinstance(container, dict) and container.get("image") != image:
            container["image"] = image
            changed = True
    return changed


def _manifest_containers(document: dict[str, Any]) -> list[dict[str, Any]]:
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "CronJob" or "jobTemplate" in spec:
        template = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    elif kind == "Job" or "template" in spec:
        template = ((spec.get("template") or {}).get("spec") or {})
    else:
        template = spec
    containers = template.get("containers") or []
    return [container for container in containers if isinstance(container, dict)]


def _ensure_kustomization_includes_cronjobs(manifest_dir: Path, cronjob_manifests: list[str] | None = None) -> bool:
    path = manifest_dir / "kustomization.yaml"
    if not path.exists():
        return False
    document = _load_yaml_file(path)
    resources = document.setdefault("resources", [])
    if not isinstance(resources, list):
        raise K8sOperatorError(f"kustomization resources must be a list: {path}")
    changed = False
    manifests = cronjob_manifests if cronjob_manifests is not None else [path.name for path in manifest_dir.glob("cronjob-*.yaml")]
    for manifest in sorted(manifests):
        if manifest not in resources:
            resources.append(manifest)
            changed = True
    if changed:
        _write_yaml_file(path, document)
    return changed


def _resource_name(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    return str(metadata.get("name") or "")


def _cronjob_status(item: dict[str, Any], expected_image: str | None) -> CronJobStatus:
    metadata = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    image = _manifest_image(item) or "unknown"
    return CronJobStatus(
        name=str(metadata.get("name") or ""),
        schedule=str(spec.get("schedule") or ""),
        suspend=bool(spec.get("suspend", False)),
        active=len(status.get("active") or []),
        last_schedule=str(status.get("lastScheduleTime") or "never"),
        image=image,
        image_drift=bool(expected_image and image != "unknown" and image != expected_image),
    )


def _job_status(item: dict[str, Any]) -> JobStatus:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    conditions = status.get("conditions") or []
    state = "running"
    for condition in conditions:
        if condition.get("status") == "True":
            state = str(condition.get("type") or state).lower()
    if status.get("failed"):
        state = "failed"
    elif status.get("succeeded"):
        state = "complete"
    return JobStatus(
        name=str(metadata.get("name") or ""),
        created_at=str(metadata.get("creationTimestamp") or ""),
        status=state,
    )


def _job_failed(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    if int(status.get("failed") or 0) > 0:
        return True
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return True
    return False


def _job_active(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    return int(status.get("active") or 0) > 0


def _pod_status(item: dict[str, Any]) -> PodStatus:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    reason = str(status.get("reason") or "")
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        waiting = state.get("waiting") or {}
        if waiting.get("reason"):
            reason = str(waiting.get("reason"))
            break
    return PodStatus(name=str(metadata.get("name") or ""), phase=str(status.get("phase") or "Unknown"), reason=reason)


def _pod_failed(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    if status.get("phase") == "Failed":
        return True
    if status.get("phase") in {"Running", "Succeeded"}:
        return False
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        terminated = state.get("terminated") or {}
        waiting = state.get("waiting") or {}
        if terminated.get("reason") == "Error" or waiting.get("reason") == "Error":
            return True
    return False


def _pod_running(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    return status.get("phase") == "Running"


def _is_agentlab_job(name: str) -> bool:
    return name.startswith("agentlab-scheduler-") or name.startswith("agentlab-doctor")


def _manifest_image(document: dict[str, Any]) -> str | None:
    containers = _manifest_containers(document)
    if not containers:
        return None
    image = containers[0].get("image")
    return str(image) if image else None


def _safe_name(value: str, label: str) -> str:
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
        raise K8sOperatorError(f"unsafe {label}: {value}")
    return value


def _safe_artifact_path(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise K8sOperatorError(f"unsafe artifact path: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise K8sOperatorError(f"unsafe artifact path: {value}")
    return value


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _reason_from_payload(payload: dict[str, Any]) -> str:
    for key in ("reason", "skipped_reason", "recommendation", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    blockers = payload.get("blockers")
    if isinstance(blockers, list) and blockers:
        return str(blockers[0])
    return ""


def _is_transient_log_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        term in lowered
        for term in (
            "containercreating",
            "podinitializing",
            "waiting to start",
        )
    )


def _doctor_status_from_logs(logs: str) -> str:
    if "AgentLab doctor: failed" in logs:
        raise K8sOperatorError("Doctor failed: AgentLab doctor: failed")
    if "AgentLab doctor: warning" in logs:
        return "warning"
    if "AgentLab doctor: passed" in logs:
        return "passed"
    raise K8sOperatorError("Doctor logs did not contain an AgentLab doctor status line.")


def _doctor_logs_have_status(logs: str) -> bool:
    return any(
        status_line in logs
        for status_line in (
            "AgentLab doctor: passed",
            "AgentLab doctor: warning",
            "AgentLab doctor: failed",
        )
    )


def _log_snippet(logs: str, *, limit: int = 500) -> str:
    snippet = " ".join(line.strip() for line in logs.splitlines() if line.strip())
    if len(snippet) > limit:
        return snippet[: limit - 3] + "..."
    return snippet


def _manifest_with_task_id(path: Path, task_id: str) -> str:
    return _manifest_with_args(path, task_id=task_id, extra_args=[])


def _manifest_with_args(path: Path, *, task_id: str | None = None, extra_args: list[str] | None = None) -> str:
    if task_id is not None:
        _validate_task_id(task_id)
    for value in extra_args or []:
        if not str(value).strip():
            raise K8sOperatorError("extra plan arguments may not be empty")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        containers = manifest["spec"]["template"]["spec"]["containers"]
        args = list(containers[0].get("args") or [])
    except (KeyError, IndexError, TypeError) as exc:
        raise K8sOperatorError(f"could not add --task-id to generated Job manifest: {path}") from exc
    if not args:
        raise K8sOperatorError(f"could not update generated Job manifest without args: {path}")
    if "--task-id" in args:
        index = args.index("--task-id")
        args = args[:index] + args[index + 2 :]
    for flag in ("--focus", "--prefer-task-type", "--prefer-task-id"):
        while flag in args:
            index = args.index(flag)
            args = args[:index] + args[index + 2 :]
    updated_args = list(args)
    if task_id is not None:
        updated_args.extend(["--task-id", task_id])
    updated_args.extend(extra_args or [])
    containers[0]["args"] = updated_args
    return yaml.safe_dump(manifest, sort_keys=False)


def _validate_task_id(task_id: str) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not task_id or any(char not in allowed for char in task_id):
        raise K8sOperatorError("task_id may only contain letters, numbers, hyphen and underscore")


def resolve_tui_choice(
    raw: str,
    choices: list[str | TuiChoice],
    *,
    case_insensitive: bool = True,
) -> str:
    normalized_choices = [_tui_choice(choice) for choice in choices]
    value = raw.strip()
    if value.isdecimal():
        index = int(value) - 1
        if 0 <= index < len(normalized_choices):
            return normalized_choices[index].value

    candidate = _normalize_tui_match(value, case_insensitive=case_insensitive)
    for choice in normalized_choices:
        aliases = (choice.value, choice.display, *choice.aliases)
        for alias in aliases:
            if _normalize_tui_match(alias, case_insensitive=case_insensitive) == candidate:
                return choice.value
    raise K8sOperatorError(_invalid_tui_selection_message(value, normalized_choices))


def _tui_choice(choice: str | TuiChoice) -> TuiChoice:
    return choice if isinstance(choice, TuiChoice) else TuiChoice(choice)


def _normalize_tui_match(value: str, *, case_insensitive: bool) -> str:
    value = value.strip()
    return value.casefold() if case_insensitive else value


def _invalid_tui_selection_message(raw: str, choices: list[TuiChoice]) -> str:
    upper = len(choices)
    values = ", ".join(choice.value for choice in choices)
    return f"Invalid selection: {raw}\nValid choices: 1-{upper} or {values}"


QUESTIONARY_TUI_STYLE_RULES = [
    ("qmark", "bold"),
    ("question", "bold"),
    ("answer", "bold"),
    ("pointer", "bold"),
    ("highlighted", "bold fg:#ffffff bg:#444444"),
    ("selected", ""),
    ("instruction", ""),
    ("text", ""),
    ("checkbox", ""),
    ("separator", ""),
    ("disabled", "fg:#888888"),
    ("shortcut", ""),
]

QUESTIONARY_TUI_INSTALL_HINT = (
    "Arrow-key TUI support requires `questionary`.\n"
    "Install it with: python -m pip install -e '.[tui]'\n"
    "Falling back to numbered input."
)


def build_questionary_tui_style(questionary_module: Any) -> Any | None:
    style_factory = getattr(questionary_module, "Style", None)
    if style_factory is None:
        return None
    return style_factory(QUESTIONARY_TUI_STYLE_RULES)


class TUIAdapter(Protocol):
    def select(self, label: str, choices: list[str | TuiChoice], *, default: str | None = None) -> str:
        ...

    def confirm(self, message: str, *, default: bool = False) -> bool:
        ...

    def text(self, label: str, *, default: str | None = None) -> str:
        ...


class FallbackTUIAdapter:
    def __init__(
        self,
        *,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
    ) -> None:
        self.input = input_func
        self.output = output_func

    def select(self, label: str, choices: list[str | TuiChoice], *, default: str | None = None) -> str:
        normalized = [_tui_choice(choice) for choice in choices]
        for index, choice in enumerate(normalized, start=1):
            self.output(f"{index}. {choice.display}")
        raw = self._read(f"{label}: ").strip()
        if not raw:
            if default is not None:
                return default
            raise K8sOperatorError(f"{label} is required.")
        return resolve_tui_choice(raw, normalized)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            raw = self._read(f"{message} {suffix} ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            self.output("Please answer y or n.")

    def text(self, label: str, *, default: str | None = None) -> str:
        prompt = f"{label}: " if default is None else f"{label} (default: {default}): "
        raw = self._read(prompt).strip()
        return default if raw == "" and default is not None else raw

    def _read(self, prompt: str) -> str:
        try:
            return self.input(prompt)
        except KeyboardInterrupt as exc:
            raise TuiCancelled("Cancelled.") from exc


class QuestionaryTUIAdapter:
    def __init__(self, questionary_module: Any, *, style: Any | None = None) -> None:
        self.questionary = questionary_module
        self.style = build_questionary_tui_style(questionary_module) if style is None else style

    def select(self, label: str, choices: list[str | TuiChoice], *, default: str | None = None) -> str:
        normalized = [_tui_choice(choice) for choice in choices]
        labels = [choice.display for choice in normalized]
        values_by_label = {choice.display: choice.value for choice in normalized}
        kwargs: dict[str, Any] = {"choices": labels}
        if self.style is not None:
            kwargs["style"] = self.style
        answer = self._ask(self.questionary.select(label, **kwargs))
        if not answer:
            if default is not None:
                return default
            raise TuiCancelled("Cancelled.")
        return values_by_label.get(str(answer), str(answer))

    def confirm(self, message: str, *, default: bool = False) -> bool:
        kwargs: dict[str, Any] = {"default": default}
        if self.style is not None:
            kwargs["style"] = self.style
        answer = self._ask(self.questionary.confirm(message, **kwargs))
        return default if answer is None else bool(answer)

    def text(self, label: str, *, default: str | None = None) -> str:
        kwargs: dict[str, Any] = {"default": default or ""}
        if self.style is not None:
            kwargs["style"] = self.style
        answer = self._ask(self.questionary.text(label, **kwargs))
        if answer is None:
            raise TuiCancelled("Cancelled.")
        value = str(answer).strip()
        return default if value == "" and default is not None else value

    def _ask(self, prompt: Any) -> Any:
        try:
            return prompt.ask()
        except KeyboardInterrupt as exc:
            raise TuiCancelled("Cancelled.") from exc


def create_tui_adapter(
    *,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    notify_missing_questionary: bool = True,
) -> TUIAdapter:
    try:
        questionary = importlib.import_module("questionary")
    except ImportError:
        if notify_missing_questionary:
            output_func(QUESTIONARY_TUI_INSTALL_HINT)
        return FallbackTUIAdapter(input_func=input_func, output_func=output_func)
    return QuestionaryTUIAdapter(questionary)


class K8sTUI:
    MAIN_CHOICES = [
        TuiChoice("status", "Status anzeigen"),
        TuiChoice("runs", "Recent runs anzeigen"),
        TuiChoice("logs", "Logs ansehen"),
        TuiChoice("run", "Job starten"),
        TuiChoice("artifact", "Artifact ansehen"),
        TuiChoice("reset-state", "Scheduler state resetten"),
        TuiChoice("suspend", "CronJob pausieren"),
        TuiChoice("resume", "CronJob fortsetzen"),
        TuiChoice("shell", "Artifact shell öffnen"),
        TuiChoice("upgrade", "Upgrade / reconcile deployment"),
        TuiChoice("cleanup", "Cleanup failed resources"),
        TuiChoice("quit", "Beenden", aliases=("exit",)),
    ]
    COMPONENT_CHOICES = ["watch", "plan", "action", "review-comments", "doctor"]
    RUN_COMPONENT_CHOICES = ["watch", "plan", "action", "review-comments", "doctor", "reset-state"]
    CRONJOB_CHOICES = ["watch", "plan", "action", "review-comments"]
    PRESERVE_CHOICES = [
        TuiChoice("none"),
        TuiChoice("local generated config"),
        TuiChoice("cluster config"),
    ]

    def __init__(
        self,
        operator: K8sOperator,
        *,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        confirm_func: Callable[[str], bool] | None = None,
        adapter: TUIAdapter | None = None,
    ) -> None:
        self.operator = operator
        self.output = output_func
        self.confirm_func = confirm_func
        self.adapter = adapter or FallbackTUIAdapter(input_func=input_func, output_func=output_func)

    def run_once(self, choice: str) -> bool:
        try:
            action = self._select_main(choice)
            if action == "status":
                self.output(format_status(self.operator.status()))
            elif action == "runs":
                if self._confirm("Create artifact-shell pod if missing to list runs?", default=True):
                    self.output(format_runs(self.operator.runs()))
            elif action == "logs":
                self._show_logs()
            elif action == "run":
                self._run_job()
            elif action == "artifact":
                self._show_artifact()
            elif action == "reset-state":
                if self._confirm("This clears scheduler state. Continue?", default=False):
                    manifest = self.operator.run_component("reset-state")
                    self.output(f"Manifest: {manifest}")
            elif action == "suspend":
                component = self._select_cronjob()
                if self._confirm(f"Suspend CronJob {cronjob_for_component(component)}?", default=False):
                    self.output(str(self.operator.set_cronjob_suspend(component, True)))
            elif action == "resume":
                component = self._select_cronjob()
                if self._confirm(f"Resume CronJob {cronjob_for_component(component)}?", default=False):
                    self.output(str(self.operator.set_cronjob_suspend(component, False)))
            elif action == "shell":
                if self._confirm("Create artifact-shell pod if missing and open shell?", default=False):
                    self.operator.shell()
            elif action == "upgrade":
                self._upgrade()
            elif action == "cleanup":
                resources = self.operator.failed_resources()
                self.output(format_failed_resources(resources, namespace=self.operator.namespace))
                if resources.found and self._confirm("Delete failed AgentLab resources?", default=False):
                    self.output(format_cleanup_report(self.operator.cleanup_failed()))
            elif action == "quit":
                return False
        except TuiCancelled:
            self.output("Cancelled.")
        except K8sOperatorError as exc:
            self.output(str(exc))
        return True

    def run(self) -> None:
        keep_running = True
        while keep_running:
            try:
                keep_running = self.run_once(self.adapter.select("Auswahl", self.MAIN_CHOICES, default="status"))
            except TuiCancelled:
                self.output("Cancelled.")
                return

    def _select_main(self, choice: str) -> str:
        return resolve_tui_choice(choice, self.MAIN_CHOICES)

    def _show_logs(self) -> None:
        component = self._select_component(include_reset=False)
        job_name = self.operator.latest_job_name(component)
        logs = self.operator.job_logs(job_name, follow=False)
        self.output(f"Selected Job: {job_name}")
        if logs:
            self.output(logs)

    def _run_job(self) -> None:
        component = self._select_component(include_reset=True)
        if component == "action":
            message = "This may create or update a Merge Request. Continue?"
        elif component == "reset-state":
            message = "This clears scheduler state. Continue?"
        else:
            message = f"Run {component} Job?"
        if self._confirm(message, default=False):
            manifest = self.operator.run_component(component)
            self.output(f"Manifest: {manifest}")

    def _show_artifact(self) -> None:
        if not self._confirm("Create artifact-shell pod if missing to read artifacts?", default=True):
            return
        self._ensure_artifact_shell()
        run_id = self._select_run_id()
        resolved_run_id = self._resolve_run_id(run_id)
        if not resolved_run_id:
            return
        self.output(f"Run ID: {resolved_run_id}")
        artifacts = self.operator.available_artifacts(resolved_run_id)
        if not artifacts:
            self.output(f"No artifacts found for run {resolved_run_id}")
            return
        self.output("Available artifacts:")
        artifact = self._select_artifact(artifacts)
        if artifact is None:
            return
        try:
            result = self.operator.artifact(resolved_run_id, artifact)
        except ArtifactNotFoundError as exc:
            self.output(str(exc))
            self.output("Available artifacts:")
            if exc.available_artifacts:
                for available in exc.available_artifacts:
                    self.output(f"- {available}")
            else:
                self.output("- none")
            return
        self.output(result.path)
        if result.content:
            self.output(result.content)

    def _ensure_artifact_shell(self) -> None:
        ensure = getattr(self.operator, "ensure_artifact_shell", None)
        if callable(ensure):
            ensure()

    def _select_run_id(self) -> str:
        try:
            runs = self.operator.runs(limit=20)
            run_ids = [item.run_id for item in runs]
        except Exception as exc:
            self.output(f"Could not list recent runs: {exc}")
            return self.adapter.text("Run ID", default="latest")
        choices = ["latest", *[run_id for run_id in run_ids if run_id != "latest"]]
        return self.adapter.select("Run ID", choices, default="latest")

    def _resolve_run_id(self, run_id: str) -> str | None:
        if run_id != "latest":
            return run_id
        try:
            return self.operator.latest_run_id()
        except K8sOperatorError as exc:
            self.output(str(exc))
            return None

    def _select_artifact(self, artifacts: list[str]) -> str | None:
        try:
            return self.adapter.select("Artifact name", artifacts)
        except K8sOperatorError as exc:
            if str(exc) == "Artifact name is required.":
                self.output("Artifact name is required.")
                return None
            raise

    def _upgrade(self) -> None:
        image = self.adapter.text("Image (example: registry.example.com/agentlab:0.1.17)").strip()
        if not image:
            self.output("Image is required. Upgrade cancelled.")
            return
        preserve_source = self._select("Preserve config", self.PRESERVE_CHOICES)
        apply = self._confirm("Apply generated manifests to the cluster?", default=False)
        run_doctor = False
        cleanup_failed = False
        if apply:
            run_doctor = self._confirm("Run doctor after apply?", default=True)
            cleanup_failed = self._confirm("Cleanup failed resources after successful apply?", default=True)
            self.output("Upgrade will apply generated manifests to the cluster.")
            self.output(f"Image: {image}")
            self.output(f"Preserve config: {preserve_source}")
            self.output(f"Run doctor: {'yes' if run_doctor else 'no'}")
            self.output(f"Cleanup failed: {'yes' if cleanup_failed else 'no'}")
            if not self._confirm("Continue?", default=False):
                self.output("Upgrade cancelled.")
                return
        report = self.operator.upgrade(
            image=image,
            apply=apply,
            preserve_local_config=preserve_source == "local generated config",
            preserve_cluster_config=preserve_source == "cluster config",
            run_doctor=run_doctor,
            cleanup_failed=cleanup_failed,
        )
        self.output(format_upgrade_report(report))

    def _select_component(self, *, include_reset: bool) -> str:
        return self._select("Component", self.RUN_COMPONENT_CHOICES if include_reset else self.COMPONENT_CHOICES)

    def _select_cronjob(self) -> str:
        return self._select("CronJob", self.CRONJOB_CHOICES)

    def _select(self, label: str, values: list[str | TuiChoice]) -> str:
        return self.adapter.select(label, values)

    def _confirm(self, message: str, *, default: bool = False) -> bool:
        try:
            if self.confirm_func is not None:
                return self.confirm_func(message)
            return self.adapter.confirm(message, default=default)
        except KeyboardInterrupt as exc:
            raise TuiCancelled("Cancelled.") from exc


def run_tui(
    operator: K8sOperator,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> None:
    if not stdin.isatty() or not stdout.isatty():
        raise TuiUnavailableError(
            "Interactive TUI requires a TTY. Use `agentlab k8s status`, "
            "`agentlab k8s logs <component>`, `agentlab k8s run <component>`, "
            "`agentlab k8s artifact latest <artifact>`, or "
            "`agentlab k8s upgrade --image <image>` instead."
        )
    K8sTUI(
        operator,
        input_func=input_func,
        output_func=output_func,
        adapter=create_tui_adapter(input_func=input_func, output_func=output_func),
    ).run()
