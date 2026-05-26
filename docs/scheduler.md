# AgentLab Scheduler Operations

AgentLab scheduler support is split into three commands:

- `scheduler-watch`: cheap GitLab API check for the default branch head and open `agent/*` merge requests.
- `scheduler-plan`: repository indexing, planning, and deterministic AutoApprovalPolicy evaluation.
- `scheduler-action`: one bounded implementation/MR attempt after scheduler limits pass.
- `scheduler-review-comments`: polling loop for explicit commands in GitLab MR notes/discussions on agent-generated MRs.

Scheduling is disabled by default. Start manually before enabling CronJobs.

## Manual Kubernetes Quickstart

Generate the Kubernetes runtime and use a numeric GitLab project ID when possible:

```bash
python scripts/bootstrap_k8s.py \
  --namespace agentlab \
  --image registry.example.com/agentlab:0.1.9 \
  --gitlab-url https://gitlab.example.com \
  --project-id "5" \
  --target-repo-url https://gitlab.example.com/re/project.git \
  --ollama-url http://127.0.0.1:11434
```

Apply base resources and run manual scheduler jobs:

```bash
kubectl apply -k deploy/kubernetes/generated
kubectl apply -f deploy/kubernetes/generated/job-scheduler-watch.yaml
kubectl -n agentlab logs job/agentlab-scheduler-watch -f

kubectl apply -f deploy/kubernetes/generated/job-scheduler-plan.yaml
kubectl -n agentlab logs job/agentlab-scheduler-plan -f
```

Reset scheduler state without a helper pod:

```bash
agentlab scheduler-reset-state --config /etc/agentlab/config.yaml
agentlab scheduler-status --config /etc/agentlab/config.yaml
```

The state path is always `<workspace_root>/scheduler/state.json`. With the Kubernetes default this is:

```text
/var/lib/agentlab/runs/scheduler/state.json
```

The path `/var/lib/agentlab/scheduler/state.json` is not used when `workspace_root` is `/var/lib/agentlab/runs`.

## Starter Config

```yaml
project_id: "5"

auto_approve:
  enabled: true
  max_risk_score: 3
  allowed_task_types:
    - docs
    - tests
  allowed_paths:
    - README.md
    - docs/**
    - tests/**
    - rust-backend/tests/**
    - web/src/**/*.test.ts
  blocked_paths:
    - .gitlab-ci.yml
    - deploy/**
    - Dockerfile
    - compose.yaml
    - "**/.env"
  max_changed_files: 5
  require_tests_for_code: true

schedule:
  enabled: true
  timezone: "Europe/Berlin"
  watch:
    enabled: true
    cron: "*/30 * * * *"
  plan:
    enabled: true
    cron: "0 7,19 * * *"
  action:
    enabled: false
    cron: "30 2 * * *"
    preferred_task_types:
      - tests
      - docs
    preferred_task_ids: []
  review_comments:
    enabled: false
    cron: "*/15 * * * *"
    process_history: false
    max_comments_per_run: 1
    cooldown_minutes: 10
    allowed_commands:
      - revise
      - fix
      - propose
      - apply
      - dry-run
      - status
      - explain
      - stop
      - resume
    allowed_authors: []
    require_author_role:
      - owner
      - maintainer

functional_test_env:
  CARGO_BUILD_JOBS: "1"
```

Keep `schedule.action.enabled: false` until watch, plan, and AutoApproval reports look right.

## MR Review Comment Commands

`scheduler-review-comments` polls GitLab MR Notes and Discussions. It is intentionally not a webhook receiver: AgentLab does not expose a new HTTP service, ingress, or webhook signature endpoint for this loop.

AgentLab only reacts when all MR filters match:

- MR is open.
- `source_branch` starts with `agent/`.
- MR has label `agent/generated`.
- `target_branch` equals `default_branch`.
- MR belongs to the configured `project_id`.

Supported commands:

```text
/agent revise
/agent fix
/agent propose
/agent apply
/agent dry-run
/agent revise --dry-run
/agent status
/agent explain
/agent stop
/agent resume

@agentlab revise
@agentlab fix
@agentlab propose
@agentlab apply
@agentlab dry-run
@agentlab status
@agentlab explain
@agentlab stop
@agentlab resume
```

`/agent revise` and `/agent fix` may update code or docs on the existing MR source branch, subject to AutoApproval, allowed paths, protected paths, risk checks, tests, and Gatekeeper. They do not create a new MR and they do not enable auto-merge or direct-main push.

`/agent propose`, `/agent dry-run`, and `/agent revise --dry-run` generate a proposed revision only. They must report `Commit: none` and `Push: skipped`, write proposal artifacts such as `proposed.diff`, and must not present proposal validation as an applied MR gate.

`/agent apply` applies the latest proposal artifact for the same MR/source branch without asking the model again. It uses `structured_proposal.json` when present, otherwise `proposed.diff`, rejects stale proposals when the source branch has moved or the artifact no longer applies cleanly, reruns policy and gate checks before commit/push, and records the proposal `run_id` in the apply report and commit message.

`/agent status`, `/agent explain`, `/agent stop`, and `/agent resume` are read-only with respect to repository files. `stop` writes only the scheduler state marker for that MR; future `revise` and `fix` commands are skipped until an authorized user posts `/agent resume`.

Examples:

```text
/agent revise
Bitte README-Struktur anhand der tatsaechlichen Dateien unter rust-backend/src/routes aktualisieren.
```

```text
/agent fix
Die Aenderung an web/package.json bitte zuruecknehmen. Der MR soll nur README.md aendern.
```

```text
/agent status
```

```text
/agent stop
Ich uebernehme diesen MR manuell.
```

Rejected commands include `/agent run`, `/agent shell`, `/agent bash`, `/agent exec`, `/agent deploy`, `/agent merge`, `/agent approve`, `/agent auto-merge`, and `/agent push-main`. They are answered with a rejection comment and never executed.

Authorization defaults to Owner/Maintainer roles. You can also configure a conservative explicit allowlist:

```yaml
schedule:
  enabled: true
  review_comments:
    enabled: true
    process_history: false
    allowed_authors:
      - alice
      - bob
    require_author_role: []
```

If role checks are unavailable and `allowed_authors` is empty, commands are blocked. Bot-authored comments are ignored to avoid loops. On the first run with empty review-comment state, historical comments are recorded under `review_comments_seen` and skipped unless `schedule.review_comments.process_history` is true. Processed notes are recorded in `<workspace_root>/scheduler/state.json` under `processed_review_comments`, so the same note is never patched or answered twice.

## Recommended Rollout

Stage 1:
- Enable `schedule.watch` and `schedule.plan`.
- Keep `schedule.action.enabled: false`.
- Enable `auto_approve`.
- Goal: test plan and policy only; no changes are implemented.

Stage 2:
- Run `job-scheduler-action.yaml` manually.
- Keep `max_open_agent_mrs` low.
- Keep `max_new_mrs_per_day: 1`.
- To run one approved task instead of the automatic selection, pass `--task-id`, for example
  `agentlab scheduler-action --config /etc/agentlab/config.yaml --task-id tests-02-smoke-baseline`
  or `agentlab k8s run action --task-id tests-02-smoke-baseline`.
- Unknown or unapproved task IDs fail and do not fall back to another task.
- To prefer a task type without requiring a specific ID, configure `schedule.action.preferred_task_types`
  or pass `agentlab scheduler-action --config /etc/agentlab/config.yaml --prefer-task-type tests`.
  Repeating `--prefer-task-type` or `--prefer-task-id` sets priority order. AgentLab only selects approved
  tasks; if no preferred approved task exists, it uses the normal automatic selection. Reports include
  `task_selection_reason`.

Stage 3:
- Generate CronJobs with `--schedule-enabled`.
- Keep action strictly limited.

CronJobs are only generated when `schedule.enabled` is true. Manual `job-scheduler-*.yaml` manifests are generated even when scheduling is disabled.

## Resource Controls

Generated Kubernetes Jobs include `backoffLimit: 0`, `activeDeadlineSeconds`, and container resource requests/limits. CronJobs use `concurrencyPolicy: Forbid` so a slow watch, plan, action, or review-comments run is not overlapped by the next scheduled tick.

Small homelab clusters should start with conservative bootstrap defaults:

```bash
python scripts/bootstrap_k8s.py \
  --job-cpu-request 250m \
  --job-memory-request 512Mi \
  --job-cpu-limit 1 \
  --job-memory-limit 2Gi \
  --job-active-deadline-seconds 3600
```

Rust functional tests default to `CARGO_BUILD_JOBS=1` through `functional_test_env` to keep Cargo compilation from saturating small nodes. Increase it only after observing stable CPU and memory headroom.

Keep `schedule.review_comments.cron` at `*/15 * * * *` or slower unless you explicitly need faster feedback. Avoid `*/1 * * * *` on homelab clusters because review polling can overlap with action jobs and functional tests even when CronJobs themselves are serialized.

## Reports

Each scheduler run writes `scheduler_report.json` in the run artifacts directory. Planning also writes:

- `plan.json`
- `approved_plan.json`
- `auto_approval_report.json`

The watch report includes `open_agent_mrs_count` and an `open_agent_mrs` detail list with MR IID, title, source branch, URL, labels, and `updated_at`. It also records lightweight feedback for closed, unmerged `agent/generated` MRs in scheduler state under `closed_agent_mr_feedback`, including MR IID, title, branch, changed files, labels, `closed_at`, and a reason when a comment contains `/agent stop reason: ...`. If listing open or closed MRs fails, watch keeps the last known state and includes a warning.

Scheduler action uses `closed_agent_mr_feedback` only as local deterministic state. It does not train a model. Approved tasks that look similar to closed feedback are lowered in priority during automatic selection; explicit `--task-id` still has to name an approved task and never falls back silently. Selection reports include `task_selection_reason` and any `task_selection_feedback_matches`.

AutoApproval rejection details include concrete files. For example, `path_not_allowed` shows:

```json
{
  "task_id": "add-rust-backend-unit-test",
  "approved": false,
  "reasons": ["path_not_allowed"],
  "details": {
    "affected_files": [
      "rust-backend/src/error.rs",
      "rust-backend/Cargo.toml"
    ],
    "disallowed_paths": [
      "rust-backend/src/error.rs"
    ],
    "matched_allowed_paths": {
      "rust-backend/Cargo.toml": "rust-backend/Cargo.toml"
    }
  }
}
```

This means `rust-backend/Cargo.toml` was allowed by policy, but `rust-backend/src/error.rs` was not. Extend `allowed_paths` only when that file class is safe for autonomous work. Dependency files and CI/deployment files should normally stay blocked or require manual review because they are supply-chain relevant.

## Rust Smoke Tests

Rust integration tests under `rust-backend/tests/` can import the package crate only when the package exposes a library target, either through `rust-backend/src/lib.rs` or a `[lib]` section in `rust-backend/Cargo.toml`.

Binary-only crates that only have `src/main.rs` need one of these explicit strategies:

- an inline unit test inside approved Rust source,
- an approved public library seam such as a minimal `src/lib.rs` plus an integration smoke test, or
- a clear skipped/failed implementation explaining that no meaningful project-specific test can be written as test-only.

AgentLab must not invent `use <package>::...` imports for binary-only crates and must not replace that with placeholder checks such as arithmetic, `CARGO_PKG_NAME`, or framework-only assertions.

## Troubleshooting

- `ContainerCreating`: wait briefly, then fetch logs again.
- `Permission denied /workspace`: planning/action jobs need the workspace mount. `scheduler-watch` is lightweight and does not clone the repository.
- Git clone auth failure: check `GITLAB_TOKEN`, `GIT_TERMINAL_PROMPT=0`, and the `GIT_CONFIG_*` credential helper.
- `404 Project Not Found`: use a numeric GitLab project ID, for example `project_id: "5"`.
- `default_branch_unchanged`: the plan was skipped because the default branch head matches the last plan. Use `scheduler-reset-state` or wait for a real repository change.
- `selected_task_id: null`: inspect `auto_approval_report.json`.
- `path_not_allowed`: compare `details.disallowed_paths` with `auto_approve.allowed_paths` and extend the narrowest safe pattern.
- Review comment ignored: confirm the MR is open, targets the default branch, uses an `agent/*` source branch, and has the `agent/generated` label.
- User not authorized: add the user to `schedule.review_comments.allowed_authors` or make sure GitLab role lookup works for Owner/Maintainer.
- Policy blocks revision: inspect `review_comment_report.json`, `parsed_command.json`, `revision_task.json`, and `auto_approval_report.json`.
- Comment already processed: the note ID is already in `processed_review_comments`; post a new comment for a new command.
- MR stopped: post `/agent resume` as an authorized user before posting `/agent revise` or `/agent fix` again.
