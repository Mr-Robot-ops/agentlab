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
Before tests, Docker build, Docker push, or Kubernetes apply, the command verifies that the generated Kubernetes manifest directory exists. Missing manifests fail early with a clear preflight error unless `--bootstrap-k8s` is used.

By default, `git pull` runs as `git pull --ff-only` to avoid merge commits or interactive Git states. Use `--no-pull-ff-only` only when you explicitly want the older plain-pull behavior.

## Dry Run

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.14 \
  --dry-run
```

Dry run prints the planned Git, test, Docker, Kubernetes upgrade, and status commands without mutating Git, Docker, or Kubernetes.

## Prepare Only

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.14 \
  --prepare-only
```

Prepare-only checks Git state, pulls code unless `--skip-git-pull` is used, verifies that Kubernetes manifests exist or bootstraps them when `--bootstrap-k8s` is supplied, and runs tests. It does not build, push, apply Kubernetes manifests, or run status verification.

## Bootstrap Missing Kubernetes Manifests

If `deploy/kubernetes/generated/` is missing, a normal release upgrade fails before tests or Docker:

```text
Kubernetes manifest dir is missing: deploy/kubernetes/generated. Run bootstrap first or use --bootstrap-k8s.
```

Use `--bootstrap-k8s` when the release command should generate manifests first:

```bash
agentlab release upgrade \
  --image 10.159.21.58:5000/agentlab:0.1.17 \
  --apply \
  --preserve-cluster-config \
  --bootstrap-k8s \
  --gitlab-url https://gitlab.example.com/ \
  --project-id 5 \
  --target-repo-url https://gitlab.example.com/group/project.git \
  --target-repo-ref main \
  --ollama-url http://ollama.example.com:11434 \
  --model qwen3.6:35b \
  --git-author-name "AgentLab Bot" \
  --git-author-email "agentlab-bot@gitlab.example.com"
```

The bootstrap step uses the current Python interpreter, equivalent to `python scripts/bootstrap_k8s.py`, and writes to `--manifest-dir`. Required bootstrap inputs are `--gitlab-url`, `--project-id`, `--target-repo-url`, and `--ollama-url`; other values use the bootstrap defaults unless supplied. Add `--schedule-enabled` when the generated runtime should include scheduler CronJobs.

## Generated Manifest Dirtiness

Generated manifests are an expected local artifact. Release upgrade allows a dirty working tree when the only dirty or untracked paths are under:

```text
deploy/kubernetes/generated/**
```

The report shows this as:

```text
Git status: generated manifests dirty allowed
```

Use `--no-allow-generated-dirty` to make generated manifest changes fail like any other dirty path. Non-generated changes still fail unless `--allow-dirty` is set.

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
- `--pull-ff-only` / `--no-pull-ff-only`: use `git pull --ff-only` by default.
- `--allow-dirty`: continue even when `git status --porcelain` reports local changes.
- `--allow-generated-dirty` / `--no-allow-generated-dirty`: allow only generated manifest dirtiness without allowing arbitrary changes.
- `--bootstrap-k8s`: generate missing Kubernetes manifests before tests/build/push.
- `--prepare-only`: run readiness checks and tests without Docker or Kubernetes mutation.
- `--docker-bin TEXT`: use another Docker-compatible binary.
- `--skip-build` or `--skip-push`: reuse an existing image or update manifests only.
- `--preserve-local-config`: preserve selected sections from local generated `configmap.yaml`.
- `--no-preserve-cluster-config`, `--no-run-doctor`, `--no-cleanup-failed`, `--no-status`: disable the apply defaults.

## Windows Note

Use `python -m pytest`, not `python3 -m pytest`. The command itself uses the current Python interpreter through `sys.executable` for the default test command.
