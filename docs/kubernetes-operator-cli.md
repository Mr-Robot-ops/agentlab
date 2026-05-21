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

## Status

Show current AgentLab Kubernetes status:

```bash
agentlab k8s status
```

Diagnose image drift between the live ConfigMap, CronJobs, and generated manifests:

```bash
agentlab k8s status --manifest-dir deploy/kubernetes/generated
```

The status command is read-only. It shows the ConfigMap image annotation, AgentLab CronJobs, recent scheduler jobs, failed jobs/pods, and image drift warnings.

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
10. Cleanup failed resources
11. Beenden

Mutating actions require confirmation. If no interactive TTY is available, the TUI fails clearly and suggests equivalent non-interactive commands.
