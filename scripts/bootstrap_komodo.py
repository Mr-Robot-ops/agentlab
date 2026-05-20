from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .bootstrap_common import write_file
except ImportError:  # pragma: no cover
    from bootstrap_common import write_file  # type: ignore


def generate_komodo(*, namespace: str = "agentlab", output_dir: str | Path = "deploy/komodo/generated") -> Path:
    out = Path(output_dir)
    write_file(out / "README.md", render_readme(namespace))
    write_file(out / "job-triggers.md", render_job_triggers(namespace))
    write_file(out / "agentlab-komodo.example.yaml", render_example(namespace))
    return out


def render_readme(namespace: str) -> str:
    return f"""# AgentLab Komodo Notes

Komodo integration is optional. These files do not configure a live Komodo connection and contain no secrets.

Use Komodo to trigger the generated Kubernetes Jobs after `scripts/bootstrap_k8s.py` has created the runtime manifests. Reuse the existing Kubernetes Secret `agentlab-secrets` in namespace `{namespace}`.

Recommended trigger targets:

- `agentlab-dry-run`
- `agentlab-plan`
- `agentlab-run-task`
- `agentlab-full-flow`

Keep GitLab tokens in Kubernetes Secrets only. Do not place tokens in Komodo resource definitions, Git, ConfigMaps, or job templates.
"""


def render_job_triggers(namespace: str) -> str:
    return f"""# Example Job Trigger Commands

Komodo can run equivalent commands or invoke these through its Kubernetes integration:

```bash
kubectl -n {namespace} delete job agentlab-dry-run --ignore-not-found
kubectl apply -f deploy/kubernetes/generated/job-dry-run.yaml

kubectl -n {namespace} delete job agentlab-plan --ignore-not-found
kubectl apply -f deploy/kubernetes/generated/job-plan.yaml

kubectl -n {namespace} create configmap agentlab-task --from-file=task.json=task.json --dry-run=client -o yaml | kubectl apply -f -
kubectl -n {namespace} delete job agentlab-run-task --ignore-not-found
kubectl apply -f deploy/kubernetes/generated/job-run-task.yaml

kubectl -n {namespace} delete job agentlab-full-flow --ignore-not-found
kubectl apply -f deploy/kubernetes/generated/job-full-flow.yaml
```

These examples intentionally avoid embedding tokens.
"""


def render_example(namespace: str) -> str:
    return f"""# Generic example only; adapt to your Komodo resource model.
agentlab:
  runtime: kubernetes
  namespace: {namespace}
  secretRef: agentlab-secrets
  jobs:
    dryRun: deploy/kubernetes/generated/job-dry-run.yaml
    plan: deploy/kubernetes/generated/job-plan.yaml
    runTask: deploy/kubernetes/generated/job-run-task.yaml
    fullFlow: deploy/kubernetes/generated/job-full-flow.yaml
  notes:
    - Komodo is optional.
    - No secrets are stored in this file.
    - GitLab tokens stay in the Kubernetes Secret.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate optional AgentLab Komodo notes.")
    parser.add_argument("--namespace", default="agentlab")
    parser.add_argument("--output-dir", default="deploy/komodo/generated")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = generate_komodo(namespace=args.namespace, output_dir=args.output_dir)
    print(f"Generated optional Komodo notes in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
