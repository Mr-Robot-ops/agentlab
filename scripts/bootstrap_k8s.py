from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .bootstrap_common import (
        TOKEN_PLACEHOLDER,
        gitlab_host,
        indent,
        project_identifier,
        render_agentlab_config,
        validate_mode,
        write_file,
        yaml_string,
    )
except ImportError:  # pragma: no cover - used when executed as a script
    from bootstrap_common import (  # type: ignore
        TOKEN_PLACEHOLDER,
        gitlab_host,
        indent,
        project_identifier,
        render_agentlab_config,
        validate_mode,
        write_file,
        yaml_string,
    )


JOB_COMMANDS = {
    "doctor": ["doctor", "--config", "/etc/agentlab/config.yaml"],
    "dry-run": ["dry-run", "--config", "/etc/agentlab/config.yaml"],
    "index": ["index", "--config", "/etc/agentlab/config.yaml"],
    "steward": ["steward", "--config", "/etc/agentlab/config.yaml"],
    "plan": ["plan", "--config", "/etc/agentlab/config.yaml"],
    "run-task": ["run-task", "--config", "/etc/agentlab/config.yaml", "--task", "/etc/agentlab/task.json"],
    "full-flow": ["full-flow", "--config", "/etc/agentlab/config.yaml"],
    "scheduler-watch": ["scheduler-watch", "--config", "/etc/agentlab/config.yaml"],
    "scheduler-plan": ["scheduler-plan", "--config", "/etc/agentlab/config.yaml"],
    "scheduler-action": ["scheduler-action", "--config", "/etc/agentlab/config.yaml"],
    "scheduler-review-comments": ["scheduler-review-comments", "--config", "/etc/agentlab/config.yaml"],
    "scheduler-reset-state": ["scheduler-reset-state", "--config", "/etc/agentlab/config.yaml"],
}


def generate_k8s(
    *,
    namespace: str,
    image: str,
    gitlab_url: str,
    target_repo_url: str,
    ollama_url: str,
    project: str | None = None,
    project_id: str | None = None,
    target_repo_ref: str = "main",
    model: str = "qwen3.6:35b",
    workspace_root: str = "/var/lib/agentlab/runs",
    storage_size: str = "5Gi",
    output_dir: str | Path = "deploy/kubernetes/generated",
    mode: str = "safe-dry-run",
    allow_dangerous_mode: bool = False,
    emit_komodo: bool = False,
    git_author_name: str = "AgentLab Bot",
    git_author_email: str = "agentlab-bot@example.local",
    schedule_enabled: bool = False,
    schedule_watch_cron: str = "*/30 * * * *",
    schedule_plan_cron: str = "0 7,19 * * *",
    schedule_action_cron: str = "30 2 * * *",
    schedule_review_comments_enabled: bool = False,
    schedule_review_comments_cron: str = "*/15 * * * *",
    job_cpu_request: str = "250m",
    job_memory_request: str = "512Mi",
    job_cpu_limit: str = "1",
    job_memory_limit: str = "2Gi",
    job_active_deadline_seconds: int = 3600,
) -> Path:
    mode = validate_mode(mode, allow_dangerous_mode=allow_dangerous_mode)
    project_value = project_identifier(project=project, project_id=project_id, target_repo_url=target_repo_url)
    out = Path(output_dir)
    config_yaml = render_agentlab_config(
        gitlab_url=gitlab_url,
        project_id=project_value,
        target_repo_url=target_repo_url,
        target_repo_ref=target_repo_ref,
        ollama_url=ollama_url,
        model=model,
        workspace_root=workspace_root,
        mode=mode,
        runtime="kubernetes",
        schedule_enabled=schedule_enabled,
        schedule_watch_cron=schedule_watch_cron,
        schedule_plan_cron=schedule_plan_cron,
        schedule_action_cron=schedule_action_cron,
        schedule_review_comments_enabled=schedule_review_comments_enabled,
        schedule_review_comments_cron=schedule_review_comments_cron,
    )

    files = {
        "namespace.yaml": render_namespace(namespace),
        "serviceaccount.yaml": render_serviceaccount(namespace),
        "pvc.yaml": render_pvc(namespace, storage_size),
        "configmap.yaml": render_configmap(namespace, image, config_yaml),
        "secret.example.yaml": render_secret_example(namespace, gitlab_url),
        "README.generated.md": render_readme(namespace),
    }
    for job_name, command in JOB_COMMANDS.items():
        files[f"job-{job_name}.yaml"] = render_job(
            namespace=namespace,
            image=image,
            job_name=job_name,
            command=command,
            git_author_name=git_author_name,
            git_author_email=git_author_email,
            cpu_request=job_cpu_request,
            memory_request=job_memory_request,
            cpu_limit=job_cpu_limit,
            memory_limit=job_memory_limit,
            active_deadline_seconds=job_active_deadline_seconds,
        )
    cron_commands: dict[str, list[str]] = {}
    cron_schedules: dict[str, str] = {}
    if schedule_enabled:
        cron_commands.update(
            {
                "scheduler-watch": ["scheduler-watch", "--config", "/etc/agentlab/config.yaml"],
                "scheduler-plan": ["scheduler-plan", "--config", "/etc/agentlab/config.yaml"],
                "scheduler-action": ["scheduler-action", "--config", "/etc/agentlab/config.yaml"],
            }
        )
        cron_schedules.update(
            {
                "scheduler-watch": schedule_watch_cron,
                "scheduler-plan": schedule_plan_cron,
                "scheduler-action": schedule_action_cron,
            }
        )
    if schedule_review_comments_enabled:
        cron_commands["scheduler-review-comments"] = ["scheduler-review-comments", "--config", "/etc/agentlab/config.yaml"]
        cron_schedules["scheduler-review-comments"] = schedule_review_comments_cron
    for name, command in cron_commands.items():
        files[f"cronjob-{name}.yaml"] = render_cronjob(
            namespace=namespace,
            image=image,
            job_name=name,
            command=command,
            cron=cron_schedules[name],
            git_author_name=git_author_name,
            git_author_email=git_author_email,
            cpu_request=job_cpu_request,
            memory_request=job_memory_request,
            cpu_limit=job_cpu_limit,
            memory_limit=job_memory_limit,
            active_deadline_seconds=job_active_deadline_seconds,
        )
    files["kustomization.yaml"] = render_kustomization(
        namespace,
        extra_resources=[f"cronjob-{name}.yaml" for name in sorted(cron_commands)],
    )
    for name, content in files.items():
        write_file(out / name, content)
    if emit_komodo:
        try:
            from .bootstrap_komodo import generate_komodo
        except ImportError:  # pragma: no cover
            from bootstrap_komodo import generate_komodo  # type: ignore

        generate_komodo(namespace=namespace, output_dir=Path("deploy/komodo/generated"))
    return out


def render_namespace(namespace: str) -> str:
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""


def render_serviceaccount(namespace: str) -> str:
    return f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: agentlab-runner
  namespace: {namespace}
automountServiceAccountToken: false
"""


def render_pvc(namespace: str, storage_size: str) -> str:
    return f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: agentlab-runs
  namespace: {namespace}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {storage_size}
"""


def render_configmap(namespace: str, image: str, config_yaml: str) -> str:
    return f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: agentlab-config
  namespace: {namespace}
  annotations:
    mr-robot-ops.github.io/agentlab-image: "{image}"
data:
  config.yaml: |
{indent(config_yaml, 4)}
"""


def render_secret_example(namespace: str, gitlab_url: str) -> str:
    host = gitlab_host(gitlab_url)
    return f"""apiVersion: v1
kind: Secret
metadata:
  name: agentlab-secrets
  namespace: {namespace}
type: Opaque
stringData:
  GITLAB_TOKEN: "{TOKEN_PLACEHOLDER}"
  netrc: |
    machine {host}
      login oauth2
      password {TOKEN_PLACEHOLDER}
"""


def render_kustomization(namespace: str, *, extra_resources: list[str] | None = None) -> str:
    resources = ["namespace.yaml", "serviceaccount.yaml", "pvc.yaml", "configmap.yaml", *(extra_resources or [])]
    resource_lines = "\n".join(f"  - {resource}" for resource in resources)
    return f"""apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: {namespace}
resources:
{resource_lines}
"""


def git_config_env(git_author_name: str, git_author_email: str) -> list[tuple[str, str]]:
    configs = [
        ("credential.helper", "!f() { echo username=oauth2; echo password=$GITLAB_TOKEN; }; f"),
        ("user.name", git_author_name),
        ("user.email", git_author_email),
    ]
    env = [("GIT_CONFIG_COUNT", str(len(configs)))]
    for index, (key, value) in enumerate(configs):
        env.append((f"GIT_CONFIG_KEY_{index}", key))
        env.append((f"GIT_CONFIG_VALUE_{index}", value))
    return env


def render_git_config_env(git_author_name: str, git_author_email: str) -> str:
    return "\n".join(
        f"            - name: {name}\n              value: {yaml_string(value)}" for name, value in git_config_env(git_author_name, git_author_email)
    )


def render_container_resources(
    *,
    cpu_request: str,
    memory_request: str,
    cpu_limit: str,
    memory_limit: str,
) -> str:
    return f"""          resources:
            requests:
              cpu: {yaml_string(cpu_request)}
              memory: {yaml_string(memory_request)}
            limits:
              cpu: {yaml_string(cpu_limit)}
              memory: {yaml_string(memory_limit)}"""


def render_job(
    *,
    namespace: str,
    image: str,
    job_name: str,
    command: list[str],
    git_author_name: str = "AgentLab Bot",
    git_author_email: str = "agentlab-bot@example.local",
    cpu_request: str = "250m",
    memory_request: str = "512Mi",
    cpu_limit: str = "1",
    memory_limit: str = "2Gi",
    active_deadline_seconds: int = 3600,
) -> str:
    args = ", ".join(f'"{item}"' for item in command)
    task_mount = ""
    task_volume = ""
    if job_name == "run-task":
        task_mount = """
            - name: task
              mountPath: /etc/agentlab/task.json
              subPath: task.json
              readOnly: true"""
        task_volume = """
        - name: task
          configMap:
            name: agentlab-task
            optional: true"""
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: agentlab-{job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: agentlab
    app.kubernetes.io/component: runner
    agentlab.io/command: {job_name}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: {active_deadline_seconds}
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      serviceAccountName: agentlab-runner
      automountServiceAccountToken: false
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: agentlab
          image: {image}
          imagePullPolicy: IfNotPresent
          command: ["agentlab"]
          args: [{args}]
          env:
            - name: GITLAB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: agentlab-secrets
                  key: GITLAB_TOKEN
            - name: GIT_TERMINAL_PROMPT
              value: "0"
{render_git_config_env(git_author_name, git_author_email)}
            - name: HOME
              value: /home/agentlab
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
{render_container_resources(cpu_request=cpu_request, memory_request=memory_request, cpu_limit=cpu_limit, memory_limit=memory_limit)}
          volumeMounts:
            - name: config
              mountPath: /etc/agentlab/config.yaml
              subPath: config.yaml
              readOnly: true
            - name: home
              mountPath: /home/agentlab
            - name: git-netrc
              mountPath: /home/agentlab/.netrc
              subPath: netrc
              readOnly: true
            - name: workspace
              mountPath: /workspace
            - name: runs
              mountPath: /var/lib/agentlab
            - name: tmp
              mountPath: /tmp{task_mount}
      volumes:
        - name: config
          configMap:
            name: agentlab-config
        - name: git-netrc
          secret:
            secretName: agentlab-secrets
            optional: true
            defaultMode: 0440
        - name: home
          emptyDir:
            sizeLimit: 256Mi
        - name: workspace
          emptyDir:
            sizeLimit: 10Gi
        - name: runs
          persistentVolumeClaim:
            claimName: agentlab-runs
        - name: tmp
          emptyDir:
            sizeLimit: 1Gi{task_volume}
"""


def render_cronjob(
    *,
    namespace: str,
    image: str,
    job_name: str,
    command: list[str],
    cron: str,
    git_author_name: str = "AgentLab Bot",
    git_author_email: str = "agentlab-bot@example.local",
    cpu_request: str = "250m",
    memory_request: str = "512Mi",
    cpu_limit: str = "1",
    memory_limit: str = "2Gi",
    active_deadline_seconds: int = 3600,
) -> str:
    job = render_job(
        namespace=namespace,
        image=image,
        job_name=job_name,
        command=command,
        git_author_name=git_author_name,
        git_author_email=git_author_email,
        cpu_request=cpu_request,
        memory_request=memory_request,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        active_deadline_seconds=active_deadline_seconds,
    )
    template = job.split("  template:\n", 1)[1]
    return f"""apiVersion: batch/v1
kind: CronJob
metadata:
  name: agentlab-{job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: agentlab
    app.kubernetes.io/component: scheduler
spec:
  schedule: {yaml_string(cron)}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  startingDeadlineSeconds: 1800
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: {active_deadline_seconds}
      template:
{indent(template, 8)}
"""


def render_readme(namespace: str) -> str:
    return f"""# Generated AgentLab Kubernetes Runtime

These files are generated for a Kubernetes-first AgentLab runtime. They contain no real GitLab token.

Apply the base runtime resources:

```bash
kubectl apply -k deploy/kubernetes/generated
```

Create the runtime Secret outside Git:

```bash
kubectl -n {namespace} create secret generic agentlab-secrets \\
  --from-literal=GITLAB_TOKEN="glpat-..." \\
  --from-literal=netrc=$'machine gitlab.local\\n  login oauth2\\n  password glpat-...'
```

The jobs also configure Git with `GIT_TERMINAL_PROMPT=0`, an HTTPS credential helper that reads
`GITLAB_TOKEN` from the Secret env, and a generic commit identity (`AgentLab Bot <agentlab-bot@example.local>`).
This prevents interactive clone prompts in non-root pods, allows agent branch commits, and avoids writing the
token into ConfigMaps or generated job YAML. The `.netrc` Secret mount remains optional and is group-readable
for the pod `fsGroup`.

Run the doctor job:

```bash
kubectl apply -f deploy/kubernetes/generated/job-doctor.yaml
kubectl -n {namespace} logs job/agentlab-doctor -f
```

Run the dry-run job:

```bash
kubectl apply -f deploy/kubernetes/generated/job-dry-run.yaml
kubectl -n {namespace} logs job/agentlab-dry-run -f
```

Useful follow-ups:

```bash
kubectl apply -f deploy/kubernetes/generated/job-index.yaml
kubectl apply -f deploy/kubernetes/generated/job-steward.yaml
kubectl apply -f deploy/kubernetes/generated/job-plan.yaml
kubectl apply -f deploy/kubernetes/generated/job-scheduler-watch.yaml
kubectl apply -f deploy/kubernetes/generated/job-scheduler-plan.yaml
kubectl apply -f deploy/kubernetes/generated/job-scheduler-review-comments.yaml
```

For `job-run-task.yaml`, create a ConfigMap named `agentlab-task` with `task.json` first.
Manual `job-scheduler-*.yaml` manifests are always generated for testing. Watch, plan, and action CronJobs
are generated when scheduling is enabled; the review-comment CronJob is generated when review-comment
scheduling is enabled.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate AgentLab Kubernetes runtime manifests.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--gitlab-url", required=True)
    parser.add_argument("--project")
    parser.add_argument("--project-id")
    parser.add_argument("--target-repo-url", required=True)
    parser.add_argument("--target-repo-ref", default="main")
    parser.add_argument("--ollama-url", required=True)
    parser.add_argument("--model", default="qwen3.6:35b")
    parser.add_argument("--workspace-root", default="/var/lib/agentlab/runs")
    parser.add_argument("--storage-size", default="5Gi")
    parser.add_argument("--output-dir", default="deploy/kubernetes/generated")
    parser.add_argument("--mode", default="safe-dry-run")
    parser.add_argument("--allow-dangerous-mode", action="store_true")
    parser.add_argument("--emit-komodo", action="store_true")
    parser.add_argument("--git-author-name", default="AgentLab Bot")
    parser.add_argument("--git-author-email", default="agentlab-bot@example.local")
    parser.add_argument("--schedule-enabled", action="store_true")
    parser.add_argument("--schedule-watch-cron", default="*/30 * * * *")
    parser.add_argument("--schedule-plan-cron", default="0 7,19 * * *")
    parser.add_argument("--schedule-action-cron", default="30 2 * * *")
    parser.add_argument("--schedule-review-comments-enabled", action="store_true")
    parser.add_argument("--schedule-review-comments-cron", default="*/15 * * * *")
    parser.add_argument("--job-cpu-request", default="250m")
    parser.add_argument("--job-memory-request", default="512Mi")
    parser.add_argument("--job-cpu-limit", default="1")
    parser.add_argument("--job-memory-limit", default="2Gi")
    parser.add_argument("--job-active-deadline-seconds", type=int, default=3600)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        out = generate_k8s(
            namespace=args.namespace,
            image=args.image,
            gitlab_url=args.gitlab_url,
            project=args.project,
            project_id=args.project_id,
            target_repo_url=args.target_repo_url,
            target_repo_ref=args.target_repo_ref,
            ollama_url=args.ollama_url,
            model=args.model,
            workspace_root=args.workspace_root,
            storage_size=args.storage_size,
            output_dir=args.output_dir,
            mode=args.mode,
            allow_dangerous_mode=args.allow_dangerous_mode,
            emit_komodo=args.emit_komodo,
            git_author_name=args.git_author_name,
            git_author_email=args.git_author_email,
            schedule_enabled=args.schedule_enabled,
            schedule_watch_cron=args.schedule_watch_cron,
            schedule_plan_cron=args.schedule_plan_cron,
            schedule_action_cron=args.schedule_action_cron,
            schedule_review_comments_enabled=args.schedule_review_comments_enabled,
            schedule_review_comments_cron=args.schedule_review_comments_cron,
            job_cpu_request=args.job_cpu_request,
            job_memory_request=args.job_memory_request,
            job_cpu_limit=args.job_cpu_limit,
            job_memory_limit=args.job_memory_limit,
            job_active_deadline_seconds=args.job_active_deadline_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Generated Kubernetes runtime in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
