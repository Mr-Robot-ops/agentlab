# AgentLab Release Upgrade

`agentlab update` is the normal one-command operator workflow on a Kubernetes host. It updates the local checkout, refreshes the editable install, builds a runtime image for the current `main` commit, and applies it to Kubernetes. Lower-level release and Kubernetes commands remain available when you need more control.

Git tags are the source of official release truth. A normal `main` commit does not bump the AgentLab version, and `agentlab update` does not create or push Git tags by default. A release version changes only when a maintainer intentionally runs a release command such as `agentlab release publish --bump-patch --tag --push-tag`, or explicitly opts into release mode with `agentlab update --release`.

## Normal Update

Preview the full update without mutating the working tree, release state, Docker, Kubernetes, or Git tags:

```bash
agentlab update --dry-run
```

Run the update:

```bash
agentlab update
```

`agentlab update` defaults to the current working directory, checks `git status`, allows only generated manifest dirtiness under `deploy/kubernetes/generated/**`, pulls `origin/main` with `git pull --ff-only`, re-checks `git status`, refreshes the editable install with the current Python interpreter, then re-execs itself so the deploy phase uses freshly installed code. It then runs the runtime update workflow with a commit-based image tag, pull-based image verification, Kubernetes apply, cluster config preservation, doctor, failed-resource cleanup, and status.

The runtime image includes `cargo` and `rustc` so AgentLab Kubernetes jobs can execute Rust functional tests when Rust test changes are detected, for example:

```bash
cd rust-backend && cargo test --package zfs-manager
```

After building a runtime image, verify the Rust toolchain with:

```bash
docker run --rm <image> cargo --version
docker run --rm <image> rustc --version
```

The image repository is inferred automatically, first from the live Kubernetes ConfigMap image annotation and then from the generated `deploy/kubernetes/generated/configmap.yaml` image annotation. If neither exists, run once with:

```bash
agentlab update --image-repository registry.example.com/agentlab
```

Default update behavior does not:

- create a Git tag
- push a Git tag
- create a GitHub Release
- require GitHub write credentials

The default image tag is the target commit short SHA:

```text
registry.example.com/agentlab:0ae4869
```

The Kubernetes version annotation is written as a runtime commit marker:

```text
mr-robot-ops.github.io/agentlab-version=commit 0ae4869
```

If a previous runtime update failed before Docker build, Docker push, or Kubernetes apply, the next `agentlab update` clears that pre-deploy state and retries cleanly. If the state reached a deploy step, `agentlab update` refuses to start a fresh run and tells you to resume or clear the state explicitly:

```bash
agentlab update --resume --dry-run
agentlab update --resume
agentlab update --clear-state
```

Useful update overrides:

```bash
agentlab update --image-repository registry.example.com/agentlab
agentlab update --repo /opt/agentlab
agentlab update --no-git-pull
agentlab update --no-self-install
agentlab update --no-tests
agentlab update --verify-image-method manifest
```

`agentlab update --dry-run` may run `git fetch origin` so it can compare local `HEAD` with `origin/main`, but it does not modify the working tree. The report shows whether the checkout is equal to, behind, ahead of, or diverged from `origin/main`. Diverged history fails clearly before planning a release.

When a command fails, the report includes the command, working directory, exit code, stdout/stderr tail, and the full log path under `.agentlab/logs/`.

## Official Releases

Official version publishing is explicit maintainer work. Use:

```bash
agentlab release publish --bump-patch --tag --push-tag
```

or, from the Kubernetes host only when you deliberately want update to publish a release:

```bash
agentlab update --release --bump-patch --tag --push-tag --dry-run
```

Git tag creation only happens in release commands or `agentlab update --release --tag`. Remote tag push only happens when `--push-tag` is supplied, and `--push-tag` requires `--tag`.

Preview a release publish without mutating Git, Docker, or Kubernetes:

```bash
agentlab release publish --bump-patch --tag --push-tag --dry-run
```

Useful release overrides:

```bash
agentlab release publish --bump-minor --tag
agentlab release publish --version v0.2.0 --tag --push-tag
agentlab release publish --bump-patch --github-release --tag --push-tag
agentlab update --release --bump-patch --tag --dry-run
```

Resume the latest failed or incomplete deploy:

```bash
agentlab release resume
```

Resume reads `.agentlab/release-state.json`, refuses to continue if `HEAD` differs from the recorded commit unless `--allow-head-mismatch` is supplied, skips steps already marked passed, preserves whether tags were enabled in the original workflow, and pushes a Git tag only when that workflow explicitly enabled tag push.

Preview only the remaining resume work:

```bash
agentlab release resume --dry-run
```

Inspect or clean local state:

```bash
agentlab release state
agentlab release state --clear-completed
```

## Lower-Level Deploy

`agentlab release deploy` remains available for compatibility and lower-level release deployment control. For normal Kubernetes runtime updates, prefer `agentlab update`.

For explicit image rollouts, rollback, or manual repair, use the Kubernetes reconciliation command directly:

```bash
agentlab k8s upgrade --image IMAGE --version VERSION --apply
```

Use `--version` only for official SemVer release annotations such as `v0.1.18`. For commit-based runtime rollouts, use `--runtime-version`:

```bash
agentlab k8s upgrade --image IMAGE --runtime-version "commit 0ae4869" --apply
```

## Lower-Level Release Upgrade

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
Verify method:   manifest
```

For `agentlab release deploy --dry-run`, the header is the same but the verify method defaults to `pull`.

## Explicit Version

```bash
agentlab release upgrade \
  --version 0.1.18 \
  --tag \
  --apply \
  --preserve-cluster-config
```

`--version` accepts `0.1.18` or `v0.1.18`. Git tags keep the leading `v`; Docker image tags omit it by default. Use `--image-tag-prefix-v` only when the registry tag should also be `v0.1.18`.

`--version` is SemVer-only. Runtime update labels such as `commit 0ae4869` are not valid release versions; use `--runtime-version` for those annotations.

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

For a manual runtime rollout using a commit-tagged image, pass `--runtime-version` instead. This writes the same Kubernetes version annotation key, but does not parse the text as SemVer and does not create or push Git tags:

```bash
agentlab release upgrade \
  --image registry.example.com/agentlab:0ae4869 \
  --runtime-version "commit 0ae4869" \
  --skip-git-pull \
  --verify-image-method pull \
  --apply \
  --preserve-cluster-config \
  --run-doctor \
  --cleanup-failed \
  --status
```

Do not pass `--version "commit 0ae4869"`; `--version` remains reserved for official SemVer releases. AgentLab never infers a release version from `--image` on this path. If both `--version` and `--runtime-version` are omitted, Kubernetes upgrade writes only `mr-robot-ops.github.io/agentlab-image` and leaves the version annotation unchanged.

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

`agentlab release deploy` pushes the Git tag after Kubernetes upgrade/status succeeds. Existing `agentlab release upgrade --push-tag` preserves its historical ordering unless you use the deploy wrapper.

## Image Verification

`agentlab release upgrade` supports:

```bash
agentlab release upgrade --verify-image --verify-image-method manifest
agentlab release upgrade --verify-image --verify-image-method pull
```

`manifest` runs:

```bash
docker manifest inspect <image>
```

`pull` runs:

```bash
docker pull <image>
```

Local/private registries may not support `docker manifest inspect` reliably even when `docker pull` succeeds. `agentlab release deploy` therefore defaults to `--verify-image-method pull`. Use `--no-verify-image` with `release upgrade` only when you have verified the image separately.

Manual fallback for older versions or unusual registry behavior:

```bash
docker pull <image>
agentlab release upgrade \
  --image <image> \
  --runtime-version "commit <sha>" \
  --skip-build \
  --skip-push \
  --no-verify-image \
  --apply \
  --preserve-cluster-config \
  --run-doctor \
  --cleanup-failed \
  --status
```

Use `--version <vMAJOR.MINOR.PATCH>` instead when the image is an official SemVer release image.

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
  --target-repo-url https://gitlab.example.com/group/project.git \
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
- `--verify-image-method manifest|pull`: choose `docker manifest inspect` or `docker pull` verification.
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
