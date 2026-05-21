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
  --image 10.159.21.58:5000/agentlab:0.1.9 \
  --gitlab-url https://gitlab.example.com \
  --project-id "5" \
  --target-repo-url https://gitlab.example.com/re/project.git \
  --ollama-url http://ollama:11434
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
  review_comments:
    enabled: false
    cron: "*/10 * * * *"
    process_history: false
    max_comments_per_run: 1
    cooldown_minutes: 10
    allowed_commands:
      - revise
      - fix
      - status
      - explain
      - stop
      - resume
    allowed_authors: []
    require_author_role:
      - owner
      - maintainer
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
/agent status
/agent explain
/agent stop
/agent resume

@agentlab revise
@agentlab fix
@agentlab status
@agentlab explain
@agentlab stop
@agentlab resume
```

`/agent revise` and `/agent fix` may update code or docs on the existing MR source branch, subject to AutoApproval, allowed paths, protected paths, risk checks, tests, and Gatekeeper. They do not create a new MR and they do not enable auto-merge or direct-main push.

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

Stage 3:
- Generate CronJobs with `--schedule-enabled`.
- Keep action strictly limited.

CronJobs are only generated when `schedule.enabled` is true. Manual `job-scheduler-*.yaml` manifests are generated even when scheduling is disabled.

## Reports

Each scheduler run writes `scheduler_report.json` in the run artifacts directory. Planning also writes:

- `plan.json`
- `approved_plan.json`
- `auto_approval_report.json`

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
