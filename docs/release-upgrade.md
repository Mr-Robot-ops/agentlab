# AgentLab Release Upgrade

`agentlab release upgrade` runs the local release workflow from one command: resolve the release version, pull code, run tests, build and push the image, update Kubernetes manifests, and optionally apply and verify the cluster.

Git tags are the source of release truth. A normal `main` commit does not bump the AgentLab version. A release version changes only when an operator intentionally runs `agentlab release upgrade --bump-patch`, `--bump-minor`, `--bump-major`, or `--version`.

## Recommended Release

```bash
agentlab release upgrade \
  --bump-patch \
  --tag \
  --push-tag \
  --apply \
  --preserve-cluster-config \
  --run-doctor \
  --cleanup-failed \
  --status
```

For a latest tag `v0.1.17`, this creates release version `v0.1.18` and image `registry.example.com/agentlab:0.1.18` when the current deployed image repository is `registry.example.com/agentlab`.

Kubernetes stores release metadata in these annotations:

- `mr-robot-ops.github.io/agentlab-image`
- `mr-robot-ops.github.io/agentlab-version`

AgentLab reads deprecated `agentlab.io/image` only as a migration fallback. It never writes that key, and it does not use `agentlab.github.io` as a fallback.

## Version Resolution

For `--bump-patch`, `--bump-minor`, and `--bump-major`, AgentLab resolves the current version in this order:

1. Latest Git tag matching `vMAJOR.MINOR.PATCH`.
2. Kubernetes annotation `mr-robot-ops.github.io/agentlab-version`.
3. Version parsed from `mr-robot-ops.github.io/agentlab-image`.
4. Version parsed from deprecated `agentlab.io/image`.

If none of those sources exists, provide `--current-version` or use explicit `--version`.

## Dry Run

```bash
agentlab release upgrade \
  --bump-patch \
  --dry-run
```

Dry run prints the resolved current version, new version, current image, new image, and planned Git, test, Docker, Kubernetes upgrade, and status commands without mutating Git, Docker, or Kubernetes.

Example report header:

```text
Current version: v0.1.17
New version:     v0.1.18
Current image:   registry.example.com/agentlab:0.1.17
New image:       registry.example.com/agentlab:0.1.18
```

## Explicit Version

```bash
agentlab release upgrade \
  --version 0.1.18 \
  --tag \
  --apply \
  --preserve-cluster-config
```

`--version` accepts `0.1.18` or `v0.1.18`. Git tags keep the leading `v`; Docker image tags omit it by default. Use `--image-tag-prefix-v` only when the registry tag should also be `v0.1.18`.

If AgentLab cannot infer the image repository from the deployed image annotation, pass it explicitly:

```bash
agentlab release upgrade \
  --version 0.1.18 \
  --image-repository registry.example.com/agentlab \
  --dry-run
```

## Manual Image Route

Explicit image deployment remains available for rollbacks or prebuilt images:

```bash
agentlab release upgrade \
  --image registry.example.com/agentlab:0.1.18 \
  --apply \
  --preserve-cluster-config
```

When `--image` is used, AgentLab preserves the existing manual route and does not create or push Git tags unless a version is provided explicitly and tag options are requested. Do not combine `--image` with `--bump-patch`, `--bump-minor`, or `--bump-major`.

If you intentionally want the manual image route to also write the release-version annotation, pass `--version` explicitly:

```bash
agentlab release upgrade \
  --image registry.example.com/agentlab:0.1.18 \
  --version v0.1.18 \
  --apply \
  --preserve-cluster-config
```

AgentLab never infers a release version from `--image` on this path. If `--version` is omitted, Kubernetes upgrade writes only `mr-robot-ops.github.io/agentlab-image` and leaves the version annotation unchanged.

## Ordering And Safety

The bump/version release flow runs in this order:

1. Resolve current and new release version.
2. Check `git status`.
3. Run `git pull --ff-only` unless `--skip-git-pull` is used.
4. Re-check `git status`.
5. Verify or bootstrap generated Kubernetes manifests.
6. Run tests.
7. Create a local Git tag only when `--tag` is set.
8. Build the Docker image.
9. Push the Docker image.
10. Verify the pushed image unless `--no-verify-image` is set.
11. Push the Git tag only when `--push-tag` is set.
12. Run the Kubernetes upgrade.
13. Run doctor, cleanup, and status when requested.

Docker build and push must pass before Kubernetes upgrade. If image verification fails, Kubernetes upgrade is not run and the Git tag is not pushed.

## Prepare Only

```bash
agentlab release upgrade \
  --bump-patch \
  --prepare-only
```

Prepare-only checks Git state, pulls code unless `--skip-git-pull` is used, verifies that Kubernetes manifests exist or bootstraps them when `--bootstrap-k8s` is supplied, and runs tests. It does not create tags, build, push, apply Kubernetes manifests, or run status verification.

## Bootstrap Missing Kubernetes Manifests

If `deploy/kubernetes/generated/` is missing, a normal release upgrade fails before tests or Docker:

```text
Kubernetes manifest dir is missing: deploy/kubernetes/generated. Run bootstrap first or use --bootstrap-k8s.
```

Use `--bootstrap-k8s` when the release command should generate manifests first:

```bash
agentlab release upgrade \
  --bump-patch \
  --bootstrap-k8s \
  --gitlab-url https://gitlab.example.com/ \
  --project-id 5 \
  --target-repo-url https://gitlab.example.com/re/project.git \
  --target-repo-ref main \
  --ollama-url http://127.0.0.1:11434 \
  --model qwen3.6:35b \
  --git-author-name "AgentLab Bot" \
  --git-author-email "agentlab-bot@example.internal"
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

## Useful Options

- `--repo PATH`: run from another checkout instead of the current directory.
- `--current-version TEXT`: override the current version used for bump calculations.
- `--image-repository TEXT`: override the Docker repository used for versioned image tags.
- `--tag` / `--no-tag`: create a local Git tag for versioned releases.
- `--push-tag` / `--no-push-tag`: push the Git tag only after image push and verification succeeds.
- `--verify-image` / `--no-verify-image`: control pushed image verification.
- `--skip-git-pull`: do not run `git pull`.
- `--pull-ff-only` / `--no-pull-ff-only`: use `git pull --ff-only` by default.
- `--allow-dirty`: continue even when `git status --porcelain` reports local changes.
- `--allow-generated-dirty` / `--no-allow-generated-dirty`: allow only generated manifest dirtiness without allowing arbitrary changes.
- `--bootstrap-k8s`: generate missing Kubernetes manifests before tests/build/push.
- `--prepare-only`: run readiness checks and tests without Git tag, Docker, or Kubernetes mutation.
- `--docker-bin TEXT`: use another Docker-compatible binary.
- `--skip-build` or `--skip-push`: reuse an existing image or update manifests only.
- `--preserve-local-config`: preserve selected sections from local generated `configmap.yaml`.
- `--no-preserve-cluster-config`, `--no-run-doctor`, `--no-cleanup-failed`, `--no-status`: disable the apply defaults.

## Windows Note

Use `python -m pytest`, not `python3 -m pytest`. The command itself uses the current Python interpreter through `sys.executable` for the default test command.
