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
    )


JOB_COMMANDS = {
    "doctor": ["doctor", "--config", "/etc/agentlab/config.yaml"],
    "dry-run": ["dry-run", "--config", "/etc/agentlab/config.yaml"],
    "index": ["index", "--config", "/etc/agentlab/config.yaml"],
    "steward": ["steward", "--config", "/etc/agentlab/config.yaml"],
    "plan": ["plan", "--config", "/etc/agentlab/config.yaml"],
    "run-task": ["run-task", "--config", "/etc/agentlab/config.yaml", "--task", "/etc/agentlab/task.json"],
    "full-flow": ["full-flow", "--config", "/etc/agentlab/config.yaml"],
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
    )

    files = {
        "namespace.yaml": render_namespace(namespace),
        "serviceaccount.yaml": render_serviceaccount(namespace),
        "pvc.yaml": render_pvc(namespace, storage_size),
        "configmap.yaml": render_configmap(namespace, image, config_yaml),
        "secret.example.yaml": render_secret_example(namespace, gitlab_url),
        "kustomization.yaml": render_kustomization(namespace),
        "README.generated.md": render_readme(namespace),
    }
    for job_name, command in JOB_COMMANDS.items():
        files[f"job-{job_name}.yaml"] = render_job(namespace=namespace, image=image, job_name=job_name, command=command)
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
    agentlab.io/image: "{image}"
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


def render_kustomization(namespace: str) -> str:
    return f"""apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: {namespace}
resources:
  - namespace.yaml
  - serviceaccount.yaml
  - pvc.yaml
  - configmap.yaml
"""


def render_job(*, namespace: str, image: str, job_name: str, command: list[str]) -> str:
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
            - name: HOME
              value: /home/agentlab
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
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
            defaultMode: 0600
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
```

For `job-run-task.yaml`, create a ConfigMap named `agentlab-task` with `task.json` first.
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
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Generated Kubernetes runtime in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
