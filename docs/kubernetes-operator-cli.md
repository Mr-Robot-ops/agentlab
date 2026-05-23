# AgentLab Kubernetes Operator CLI

AgentLab includes a Kubernetes operator CLI for common runtime operations without long manual `kubectl` command sequences.

The non-interactive CLI is the primary interface:

```bash
agentlab k8s <command>
```

The interactive menu is a convenience wrapper over the same helpers:

```bash
agentlab k8s tui
```

All commands default to the `agentlab` namespace. Use `--namespace` to override it.

Help is available through either form:

```bash
agentlab k8s --help
agentlab k8s help
```

## Status

Show current AgentLab Kubernetes status:

```bash
agentlab k8s status
```

Diagnose image drift between the live ConfigMap, CronJobs, and generated manifests:

```bash
agentlab k8s status --manifest-dir deploy/kubernetes/generated
```

The status command is read-only. It shows the ConfigMap image annotation, open Agent merge requests, AgentLab CronJobs, recent scheduler jobs, failed jobs/pods, and image drift warnings. If GitLab is unavailable, cluster status still renders and includes a warning for the open-MR section.

## Health

Show one compact health summary for runtime, scheduler, GitLab, models, doctor, and open Agent merge requests:

```bash
agentlab k8s health
agentlab k8s health --json
```

The health command combines live status, generated-manifest image drift, failed AgentLab jobs/pods, open Agent MRs, scheduler state age, last watch/plan/action/review runs, doctor status, model configuration, review-comment authorization settings, and whether scheduler action is enabled. It reads the scheduler state from the `agentlab-runs` PVC through the controlled `artifact-shell` pod, creating that pod if needed. It does not print GitLab tokens.

## Merge Requests

List AgentLab-generated GitLab merge requests:

```bash
agentlab k8s mrs
agentlab k8s mrs --state opened
agentlab k8s mrs --label agent/generated
agentlab k8s mrs --json
```

The command reads `gitlab_url`, `project_id`, and the token environment key from the live `agentlab-config` ConfigMap, then reads the GitLab token from the `agentlab-secrets` Kubernetes Secret by default. It only lists merge requests on AgentLab source branches with the requested label, and it never prints the token.

## ConfigMap Settings

Read selected AgentLab `config.yaml` values from the live `agentlab-config` ConfigMap:

```bash
agentlab k8s config get schedule.action.enabled
agentlab k8s config get schedule.review_comments.enabled
```

Update selected scheduler settings without editing YAML by hand:

```bash
agentlab k8s config set schedule.action.enabled true
agentlab k8s config set schedule.action.enabled false
agentlab k8s config set schedule.review_comments.cooldown_minutes 0
```

The command validates paths against an allowlist, supports bool, int, and string values, prints before/after values, and patches only `data.config.yaml`. It does not read or modify Secrets, and it leaves ConfigMap metadata and annotations such as `mr-robot-ops.github.io/agentlab-image` untouched.

## Logs

Show the latest review-comment processor logs:

```bash
agentlab k8s logs review-comments
```

Show latest action logs:

```bash
agentlab k8s logs action
```

Supported components are `review-comments`, `action`, `plan`, `watch`, `doctor`, and `latest`.

Useful options:

```bash
agentlab k8s logs review-comments --tail 200
agentlab k8s logs action --no-follow
```

## Run Jobs

Run the review comment processor from the generated manifest:

```bash
agentlab k8s run review-comments
```

Run other generated jobs:

```bash
agentlab k8s run watch
agentlab k8s run plan
agentlab k8s run action
agentlab k8s run doctor
```

The command deletes the fixed-name Job if it already exists, applies the generated manifest from `deploy/kubernetes/generated`, and streams logs by default. It does not generate ad-hoc Job YAML and does not use `/tmp` manifests.

## Upgrade Generated Manifests

Update all generated manifests to a new AgentLab image:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.13
```

Apply the generated manifests after updating them:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.13 --apply
```

Optionally write the release-version annotation at the same time:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.18 --version v0.1.18
```

Preserve operator-tuned config from the live cluster ConfigMap:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.13 --preserve-cluster-config --apply
```

Preserve operator-tuned config from the local generated `configmap.yaml` when the cluster is not reachable:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.13 --preserve-local-config
```

Run the doctor job and clean stale failed resources after a successful apply:

```bash
agentlab k8s upgrade --image 10.159.21.58:5000/agentlab:0.1.13 --apply --run-doctor --cleanup-failed
```

The upgrade command updates `configmap.yaml` annotations `mr-robot-ops.github.io/agentlab-image` and, when supplied by release upgrade, `mr-robot-ops.github.io/agentlab-version`; it also updates all generated `job-*.yaml` and `cronjob-*.yaml` container images and ensures enabled generated CronJobs are included in `kustomization.yaml`. Existing clusters using the deprecated `agentlab.io/image` annotation remain readable during migration, but generated manifests and upgrades write only the new `mr-robot-ops.github.io` annotation keys. If preserved config enables a CronJob such as `schedule.review_comments.enabled`, upgrade recreates the missing generated CronJob manifest from the matching generated Job manifest and applies enabled CronJob manifests after `kubectl apply -k`. It can preserve `auto_approve`, `schedule`, `schedule.review_comments`, `schedule.limits`, `schedule.behavior`, and `required_test_commands`. It does not preserve image annotations, Secrets, GitLab tokens, `auto_merge_enabled`, or `direct_main_push_enabled`.

## Artifacts

Show the latest proposed diff:

```bash
agentlab k8s artifact latest proposed.diff
```

Show a specific run artifact:

```bash
agentlab k8s artifact b9c483f7c10f4a5b807e8d626b664574 gate_decision.json
```

Artifact commands ensure an `artifact-shell` pod exists with `busybox:1.36` and the `agentlab-runs` PVC mounted at `/var/lib/agentlab`. If the artifact is missing, the CLI exits non-zero and lists available artifacts for that run.

## Runs

List recent AgentLab run directories:

```bash
agentlab k8s runs
agentlab k8s runs --limit 20
```

The command excludes the `scheduler` directory and best-effort reads status/reason from available run artifacts.

## Shell

Open an interactive shell in the artifact-shell pod:

```bash
agentlab k8s shell
```

This may create the controlled `artifact-shell` pod if it is missing.

## Reset Scheduler State

Reset scheduler state with the generated reset-state Job:

```bash
agentlab k8s reset-state
```

This is an alias for running `reset-state` through the generated `job-scheduler-reset-state.yaml` manifest. If the manifest is missing, rerun Kubernetes bootstrap.

## Cleanup Failed Jobs And Pods

Preview old failed AgentLab Jobs and Pods without deleting anything:

```bash
agentlab k8s cleanup-failed --dry-run
```

Delete failed AgentLab Jobs and failed AgentLab Pods after confirmation:

```bash
agentlab k8s cleanup-failed
```

Skip the confirmation prompt:

```bash
agentlab k8s cleanup-failed --yes
```

The cleanup command only targets failed resources in the selected namespace whose names start with `agentlab-`. It never deletes CronJobs, PVCs, Secrets, ConfigMaps, ServiceAccounts, running Pods, active Jobs, completed Pods without failure, or non-AgentLab resources.

## Suspend And Resume CronJobs

Pause a noisy review-comment CronJob:

```bash
agentlab k8s suspend review-comments
```

Resume it:

```bash
agentlab k8s resume review-comments
```

Supported CronJob shortcuts are `review-comments`, `action`, `plan`, and `watch`.

## TUI

Open the interactive menu:

```bash
agentlab k8s tui
```

When `questionary` is installed, the TUI uses arrow-key selection and Enter for menus. You can verify arrow-key mode by the `Use arrow keys` hint in the prompt.

For editable/dev installs, install the optional TUI extra from the repository root:

```bash
python -m pip install -e '.[tui]'
```

For a normal source checkout install, use:

```bash
python -m pip install '.[tui]'
```

If you only need the prompt dependency, install it directly:

```bash
python -m pip install questionary
```

You can check support without launching the TUI:

```bash
agentlab k8s tui-check
```

To install the optional extra intentionally with the current Python interpreter:

```bash
agentlab k8s tui-check --install
```

Without `questionary`, AgentLab prints a one-time hint and uses the numbered fallback prompt. The fallback accepts either the displayed number or a short command name:

```text
status
runs
logs
run
artifact
reset-state
suspend
resume
shell
upgrade
cleanup
quit
exit
```

The TUI provides:

1. Status anzeigen
2. Recent runs anzeigen
3. Logs ansehen
4. Job starten
5. Artifact ansehen
6. Scheduler state resetten
7. CronJob pausieren
8. CronJob fortsetzen
9. Artifact shell öffnen
10. Upgrade / reconcile deployment
11. Cleanup failed resources
12. Beenden

Component prompts accept either the arrow-key selection, number, or exact component name. For logs, valid names are `watch`, `plan`, `action`, `review-comments`, and `doctor`. For job runs, `reset-state` is also valid.

Artifact lookup offers `latest` plus recent run IDs. In fallback mode, the run ID defaults to `latest` when the prompt is left empty. After resolving `latest`, the TUI lists available artifacts so operators do not need to guess file names:

```text
artifact
Run ID: latest
Run ID: e474a44a82dc4bf8b6b8ce2732194ffc
Available artifacts:
1. manifest.json
2. gate_decision.json
3. raw_patch.diff
Artifact name: gate_decision.json
```

Upgrade requires a non-empty image before any generated manifest can be changed:

```text
upgrade
Image (example: 10.159.21.58:5000/agentlab:0.1.17): 10.159.21.58:5000/agentlab:0.1.17
Preserve config: cluster config
Apply generated manifests to the cluster? [y/N] y
```

When apply is selected, the TUI asks whether to run doctor and clean up failed resources, then prints a summary and asks for final confirmation before calling upgrade. Confirmation prompts make the default explicit: `[Y/n]` means Enter selects yes, and `[y/N]` means Enter selects no. Convenience prompts such as creating the artifact-shell pod default to yes; risky or mutating prompts such as apply, action jobs, reset-state, CronJob suspend/resume, shell, and failed-resource deletion default to no. Run doctor and cleanup-after-apply prompts default to yes after an apply is requested.

If no interactive TTY is available, the TUI fails clearly and suggests equivalent non-interactive commands.
