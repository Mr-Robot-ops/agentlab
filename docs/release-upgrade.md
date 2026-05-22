# AgentLab Release Upgrade

`agentlab release upgrade` runs the local release workflow from one command: pull code, run tests, build and push the image, update Kubernetes manifests, and optionally apply and verify the cluster.

## Normal Safe Workflow

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.14 \
  --apply \
  --preserve-cluster-config
```

When `--apply` is used, the command defaults to preserving cluster config, running doctor, cleaning failed resources, and checking status unless explicitly disabled.

## Dry Run

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.14 \
  --dry-run
```

Dry run prints the planned Git, test, Docker, Kubernetes upgrade, and status commands without mutating Git, Docker, or Kubernetes.

## Skip Tests

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.14 \
  --skip-tests
```

Use this only when tests were already run elsewhere. If tests fail during the normal workflow, Docker build, Docker push, and Kubernetes upgrade are not run.

## Useful Options

- `--repo PATH`: run from another checkout instead of the current directory.
- `--skip-git-pull`: do not run `git pull`.
- `--allow-dirty`: continue even when `git status --porcelain` reports local changes.
- `--docker-bin TEXT`: use another Docker-compatible binary.
- `--skip-build` or `--skip-push`: reuse an existing image or update manifests only.
- `--preserve-local-config`: preserve selected sections from local generated `configmap.yaml`.
- `--no-preserve-cluster-config`, `--no-run-doctor`, `--no-cleanup-failed`, `--no-status`: disable the apply defaults.

## Windows Note

Use `python -m pytest`, not `python3 -m pytest`. The command itself uses the current Python interpreter through `sys.executable` for the default test command.
