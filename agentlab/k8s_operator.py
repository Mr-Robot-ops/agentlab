from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

import yaml


DEFAULT_NAMESPACE = "agentlab"
DEFAULT_MANIFEST_DIR = Path("deploy/kubernetes/generated")
RUNS_ROOT = "/var/lib/agentlab/runs"
ARTIFACT_SHELL_IMAGE = "busybox:1.36"

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

    def stream(self, args: list[str]) -> int:
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

    def stream(self, args: list[str]) -> int:
        return subprocess.run(["kubectl", *args], check=False).returncode

    def interactive(self, args: list[str]) -> int:
        return subprocess.run(["kubectl", *args], check=False).returncode


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
    cronjobs: list[CronJobStatus] = field(default_factory=list)
    recent_jobs: list[JobStatus] = field(default_factory=list)
    failed_jobs: list[JobStatus] = field(default_factory=list)
    failed_pods: list[PodStatus] = field(default_factory=list)
    manifest_configmap_image: str | None = None
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


class K8sOperator:
    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        manifest_dir: str | Path = DEFAULT_MANIFEST_DIR,
        runner: KubectlRunner | None = None,
    ) -> None:
        self.namespace = namespace
        self.manifest_dir = Path(manifest_dir)
        self.runner = runner or SubprocessKubectlRunner()

    def status(self, *, manifest_dir: str | Path | None = None) -> ClusterStatus:
        configmap = self._run_json(["get", "configmap", "agentlab-config", "-o", "json"], check=False)
        configmap_image = _configmap_image(configmap)
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
            cronjobs=cronjobs,
            recent_jobs=agentlab_jobs[:10],
            failed_jobs=failed_jobs,
            failed_pods=failed_pods,
        )
        if manifest_dir is not None:
            status.manifest_configmap_image, status.manifest_image_drifts = detect_manifest_image_drift(Path(manifest_dir))
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
            args.append("-f")
            code = self._stream(args)
            if code:
                raise K8sOperatorError(f"kubectl logs failed with exit code {code}")
            return ""
        return self._run(args)

    def run_component(self, component: str, *, follow: bool = True) -> str:
        manifest = manifest_for_component(component, self.manifest_dir)
        if not manifest.exists():
            raise MissingManifestError(
                f"missing manifest: {manifest}. Re-run Kubernetes bootstrap to generate {manifest.name}."
            )
        job_name = run_job_name_for_component(component)
        self._run(["delete", "job", job_name, "--ignore-not-found=true"])
        self._run(["apply", "-f", str(manifest)])
        if follow:
            code = self._stream(["logs", f"job/{job_name}", "-f"])
            if code:
                raise K8sOperatorError(f"kubectl logs failed with exit code {code}")
        return str(manifest)

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
                self._run(["delete", "job", job])
                deleted_jobs.append(job)
            for pod in resources.pods:
                self._run(["delete", "pod", pod])
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
                self.run_component("doctor", follow=True)
                report.doctor_status = "completed"
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

    def _stream(self, args: list[str]) -> int:
        return self.runner.stream(self._ns(args))

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
    annotations["agentlab.io/image"] = image
    config = _config_from_configmap_document(configmap)
    if preserved_config:
        preserved_sections = _merge_preserved_config_sections(config, preserved_config)
    config["auto_merge_enabled"] = False
    config["direct_main_push_enabled"] = False
    data = configmap.setdefault("data", {})
    data["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
    _write_yaml_file(configmap_path, configmap)
    updated_manifests.append(configmap_path.name)

    for path in sorted([*manifest_dir.glob("job-*.yaml"), *manifest_dir.glob("cronjob-*.yaml")]):
        document = _load_yaml_file(path)
        if _set_manifest_image(document, image):
            _write_yaml_file(path, document)
            updated_manifests.append(path.name)

    if _ensure_kustomization_includes_cronjobs(manifest_dir):
        updated_manifests.append("kustomization.yaml")
    return {"updated_manifests": updated_manifests, "preserved_sections": preserved_sections}


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
    if status.manifest_configmap_image:
        lines.extend(["", f"Generated manifest ConfigMap image: {status.manifest_configmap_image}"])
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
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
    if report.cleanup_report is not None:
        lines.append(f"- cleanup deleted jobs: {len(report.cleanup_report.deleted_jobs)}")
        lines.append(f"- cleanup deleted pods: {len(report.cleanup_report.deleted_pods)}")
    return "\n".join(lines)


def _configmap_image(configmap: dict[str, Any]) -> str | None:
    metadata = configmap.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    image = annotations.get("agentlab.io/image")
    return str(image) if image else None


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


def _ensure_kustomization_includes_cronjobs(manifest_dir: Path) -> bool:
    path = manifest_dir / "kustomization.yaml"
    if not path.exists():
        return False
    document = _load_yaml_file(path)
    resources = document.setdefault("resources", [])
    if not isinstance(resources, list):
        raise K8sOperatorError(f"kustomization resources must be a list: {path}")
    changed = False
    for cronjob in sorted(manifest_dir.glob("cronjob-*.yaml")):
        if cronjob.name not in resources:
            resources.append(cronjob.name)
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


class K8sTUI:
    def __init__(
        self,
        operator: K8sOperator,
        *,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        confirm_func: Callable[[str], bool] | None = None,
    ) -> None:
        self.operator = operator
        self.input = input_func
        self.output = output_func
        self.confirm_func = confirm_func

    def run_once(self, choice: str) -> bool:
        if choice == "1":
            self.output(format_status(self.operator.status()))
        elif choice == "2":
            if self._confirm("Create artifact-shell pod if missing to list runs?"):
                self.output(format_runs(self.operator.runs()))
        elif choice == "3":
            component = self._select_component(include_reset=False)
            job, logs = self.operator.logs(component, follow=False)
            self.output(f"Selected Job: {job}")
            if logs:
                self.output(logs)
        elif choice == "4":
            component = self._select_component(include_reset=True)
            if component == "action":
                message = "This may create or update a Merge Request. Continue?"
            elif component == "reset-state":
                message = "This clears scheduler state. Continue?"
            else:
                message = f"Run {component} Job?"
            if self._confirm(message):
                manifest = self.operator.run_component(component)
                self.output(f"Manifest: {manifest}")
        elif choice == "5":
            if self._confirm("Create artifact-shell pod if missing to read artifacts?"):
                run_id = self.input("Run ID (or latest): ").strip() or "latest"
                artifact = self.input("Artifact: ").strip()
                result = self.operator.artifact(run_id, artifact)
                self.output(result.path)
                self.output(result.content)
        elif choice == "6":
            if self._confirm("This clears scheduler state. Continue?"):
                manifest = self.operator.run_component("reset-state")
                self.output(f"Manifest: {manifest}")
        elif choice == "7":
            component = self._select_cronjob()
            if self._confirm(f"Suspend CronJob {cronjob_for_component(component)}?"):
                self.output(str(self.operator.set_cronjob_suspend(component, True)))
        elif choice == "8":
            component = self._select_cronjob()
            if self._confirm(f"Resume CronJob {cronjob_for_component(component)}?"):
                self.output(str(self.operator.set_cronjob_suspend(component, False)))
        elif choice == "9":
            if self._confirm("Create artifact-shell pod if missing and open shell?"):
                self.operator.shell()
        elif choice == "10":
            image = self.input("Image: ").strip()
            preserve_source = self._select("Preserve config", ["none", "local generated config", "cluster config"])
            apply = self._confirm("Apply generated manifests to the cluster?")
            run_doctor = self._confirm("Run doctor after apply?")
            cleanup_failed = self._confirm("Cleanup failed resources after apply?")
            report = self.operator.upgrade(
                image=image,
                apply=apply,
                preserve_local_config=preserve_source == "local generated config",
                preserve_cluster_config=preserve_source == "cluster config",
                run_doctor=run_doctor,
                cleanup_failed=cleanup_failed,
            )
            self.output(format_upgrade_report(report))
        elif choice == "11":
            resources = self.operator.failed_resources()
            self.output(format_failed_resources(resources, namespace=self.operator.namespace))
            if resources.found and self._confirm("Delete failed AgentLab resources?"):
                self.output(format_cleanup_report(self.operator.cleanup_failed()))
        elif choice == "12":
            return False
        else:
            self.output("Unknown selection.")
        return True

    def run(self) -> None:
        keep_running = True
        while keep_running:
            self.output(
                "\n".join(
                    [
                        "1. Status anzeigen",
                        "2. Recent runs anzeigen",
                        "3. Logs ansehen",
                        "4. Job starten",
                        "5. Artifact ansehen",
                        "6. Scheduler state resetten",
                        "7. CronJob pausieren",
                        "8. CronJob fortsetzen",
                        "9. Artifact shell öffnen",
                        "10. Upgrade / reconcile deployment",
                        "11. Cleanup failed resources",
                        "12. Beenden",
                    ]
                )
            )
            keep_running = self.run_once(self.input("Auswahl: ").strip())

    def _select_component(self, *, include_reset: bool) -> str:
        values = ["watch", "plan", "action", "review-comments", "doctor"]
        if include_reset:
            values.append("reset-state")
        return self._select("Component", values)

    def _select_cronjob(self) -> str:
        return self._select("CronJob", ["watch", "plan", "action", "review-comments"])

    def _select(self, label: str, values: list[str]) -> str:
        for index, value in enumerate(values, start=1):
            self.output(f"{index}. {value}")
        raw = self.input(f"{label}: ").strip()
        try:
            return values[int(raw) - 1]
        except (ValueError, IndexError) as exc:
            raise K8sOperatorError(f"invalid {label} selection: {raw}") from exc

    def _confirm(self, message: str) -> bool:
        if self.confirm_func is not None:
            return self.confirm_func(message)
        return self.input(f"{message} [y/N] ").strip().lower() in {"y", "yes"}


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
            "`agentlab k8s logs <component>`, or `agentlab k8s run <component>` instead."
        )
    K8sTUI(operator, input_func=input_func, output_func=output_func).run()
