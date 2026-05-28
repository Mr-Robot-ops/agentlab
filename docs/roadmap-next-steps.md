# AgentLab Next-Step Roadmap

This roadmap captures the next small, reviewable AgentLab development steps after the recent runtime, Kubernetes, Rust, planner, and review-comment hardening work.

## Executive Summary

### Current System Status

AgentLab is in a usable operator mode for Kubernetes-hosted runtime updates and manual scheduler operation:

- `agentlab update` is the normal runtime update path. It uses commit-based image tags, does not create or push Git tags by default, and keeps official release publishing separate.
- Kubernetes generated jobs have concurrency and resource safeguards, including `concurrencyPolicy: Forbid`, job deadlines/backoff limits, PATH propagation for Rust tooling, and resource profile presets.
- The runtime image supports modern Rust tooling and functional Rust tests.
- Rust binary-only crate planning is safer: AgentLab avoids invalid integration-test imports when no library crate exists.
- Planner focus and preference hints exist for manual planning.
- `watch` and `review-comments` are the right always-on schedulers for current operations.
- `plan` and `action` should remain manual until planning dedupe, preview, and action UX are stronger.

### Stable Areas

- Runtime update semantics are now clear enough to treat as stable.
- Release/tag behavior is separated from normal Kubernetes-host updates.
- Rust toolchain availability and PATH are stable enough for repeated cargo test execution.
- TestQualityAgent blocks obvious placeholder tests and warns on weak public seams.
- `/agent merge-status` exists as a read-only operator command for MR readiness checks.
- Missing GitLab token handling for Kubernetes status no longer breaks Kubernetes-only status output.

### Risky Or Incomplete Areas

- Planner dedupe after merged Agent MRs is still the highest-risk operator annoyance. Repeated low-risk docs tasks can distract from unresolved technical follow-ups.
- Planner output is not yet review-friendly enough for fully confident manual action runs.
- Manual `agentlab k8s run action` needs clearer task-ID diagnostics when a task is missing or stale.
- Review-comments remains intentionally frequent, but completed job/pod history can become noisy.
- Cargo builds still cost time on first run and need a safe cache strategy for repeated Rust test runs.
- Doctor failures after successful update can create noisy failed resources and should be reported as warning-only unless configured as a hard gate.
- A compact day-to-day Kubernetes ops status command is still useful even though full `k8s status` and `k8s health` exist.

## Prioritized Backlog

### 1. Merge-Aware Planner Deduplication

- **Priority:** P0
- **Area:** planner
- **Problem statement:** AgentLab can keep proposing docs or test-instruction tasks after equivalent Agent-generated MRs have already merged. This creates operator noise and can hide the real unresolved technical task.
- **Proposed solution:** Add normalized task fingerprints from task type, title/description tokens, and affected files. Feed recent merged Agent MRs into planner context as completed work, separate from closed-failed feedback. Suppress near-duplicate docs tasks unless new evidence appears.
- **Expected files likely to change:** `agentlab/agents/planner.py`, `agentlab/scheduler.py`, `agentlab/models.py`, `tests/test_planner.py`, `tests/test_scheduler_watch.py`, `docs/scheduler.md`.
- **Test plan:** Add planner tests for merged README credentials/test-command MRs suppressing duplicates, unrelated docs still allowed, and Rust focus still winning when requested.
- **Risk level:** Medium.
- **Why now / why later:** Do this now because it directly improves plan quality while plan/action remain manual. It should precede any automation increase.

### 2. Distinguish Merged MR Feedback From Failed Closed Feedback

- **Priority:** P0
- **Area:** planner
- **Problem statement:** Merged Agent MRs should be treated as completed work, not negative feedback. Closed unmerged or gate-blocked MRs should remain a negative signal.
- **Proposed solution:** Store recent merged Agent MR metadata separately with iid, title, merge commit SHA, changed files, and labels. Preserve closed-failed feedback for repeated failure avoidance.
- **Expected files likely to change:** `agentlab/scheduler.py`, `agentlab/models.py`, `agentlab/agents/planner.py`, `tests/test_scheduler_watch.py`, `tests/test_planner.py`.
- **Test plan:** Add tests for merged MR recorded as completed, closed unmerged MR recorded as failed feedback, merged docs MR suppressing duplicate docs task, and closed failed Rust MR driving public-seam follow-up.
- **Risk level:** Medium.
- **Why now / why later:** Do this with or just before planner dedupe because the two features share the same planning signal model.

### 3. Plan Preview Artifact And Concise Plan Summary

- **Priority:** P1
- **Area:** UX
- **Problem statement:** Operators need a quick way to review which task AgentLab selected before running manual action.
- **Proposed solution:** Print selected task ID, title, risk score, affected files, why selected, and the copy-paste action command. Write `plan_preview.md` with approved, rejected, and blocked tasks plus focus/preference hints.
- **Expected files likely to change:** `agentlab/scheduler.py`, `agentlab/agents/planner.py`, `agentlab/models.py`, `tests/test_planner.py`, `tests/test_k8s_cli.py`, `docs/scheduler.md`.
- **Test plan:** Verify selected task summary is present, `plan_preview.md` includes all tasks and policy blockers, and focus/preference hints are visible.
- **Risk level:** Low.
- **Why now / why later:** Do this before making action easier to run. It improves operator confidence without changing behavior.

### 4. Safer Manual Action Task UX

- **Priority:** P1
- **Area:** UX
- **Problem statement:** When a manual action task ID is missing, stale, or not approved, operators need a precise failure message and a safe next command.
- **Proposed solution:** On unknown or unavailable `--task-id`, print requested ID, approved task IDs, approved plan source, and a suggested command using the selected approved task. Never fall back silently.
- **Expected files likely to change:** `agentlab/scheduler.py`, `agentlab/k8s_operator.py`, `agentlab/k8s_cli.py`, `tests/test_scheduler_action.py`, `tests/test_k8s_cli.py`, `docs/scheduler.md`.
- **Test plan:** Add tests for unknown task ID, no approved tasks, exactly one approved task, and no silent fallback.
- **Risk level:** Low.
- **Why now / why later:** Do this after plan preview so action failures point back to a richer plan artifact.

### 5. Compact Kubernetes Ops Status

- **Priority:** P1
- **Area:** k8s
- **Problem statement:** `agentlab k8s status` is useful but verbose for daily "is it safe?" checks.
- **Proposed solution:** Add `agentlab k8s ops-status` with runtime image/version, open Agent MRs, failed and active jobs/pods, scheduler states, and a recommendation. Continue showing Kubernetes status even without a GitLab token.
- **Expected files likely to change:** `agentlab/k8s_cli.py`, `agentlab/k8s_operator.py`, `tests/test_k8s_cli.py`, `docs/kubernetes-operator-cli.md`.
- **Test plan:** Cover stable state, failed doctor job, missing GitLab token, and plan/action paused states.
- **Risk level:** Low.
- **Why now / why later:** Good near-term operator value. Keep it separate from scheduler behavior changes.

### 6. Review-Comments Job History Cleanup

- **Priority:** P1
- **Area:** k8s
- **Problem statement:** Review-comments should stay frequent for `/agent ...` responsiveness, but completed jobs/pods can clutter the namespace.
- **Proposed solution:** Keep the `*/1` schedule when operators choose it, but set low successful job history for review-comments and preserve failed history for debugging. Ensure TTL is present where applicable.
- **Expected files likely to change:** `scripts/bootstrap_k8s.py`, `agentlab/k8s_operator.py`, `tests/test_runtime_bootstrap.py`, `tests/test_k8s_operator.py`, `docs/scheduler.md`.
- **Test plan:** Assert review-comments CronJob has `successfulJobsHistoryLimit: 1`, failed history present, resources present, PATH present, and `concurrencyPolicy: Forbid`.
- **Risk level:** Low.
- **Why now / why later:** Useful for cluster cleanliness. It should not slow review-comment responsiveness.

### 7. Cargo Cache For Functional Rust Tests

- **Priority:** P1
- **Area:** runtime
- **Problem statement:** Rust tests now run, but cold cargo builds download and compile many crates, which is slow on small clusters.
- **Proposed solution:** Add optional PVC-backed Cargo cache support with `functional_tests.cargo_cache_enabled`, `cargo_cache_pvc`, and `cargo_build_jobs`. Mount cache paths without polluting the source workspace.
- **Expected files likely to change:** `agentlab/config.py`, `scripts/bootstrap_k8s.py`, `agentlab/k8s_operator.py`, `agentlab/agents/test_functional.py`, `tests/test_k8s_cli.py`, `tests/test_release_upgrade.py`, `docs/kubernetes-operator-cli.md`.
- **Test plan:** Verify env variables and PVC mounts when enabled, disabled behavior remains safe, and `CARGO_BUILD_JOBS=1` remains easy for small clusters.
- **Risk level:** Medium.
- **Why now / why later:** Do after resource-profile work is merged and observed. It touches Kubernetes runtime shape, so keep it isolated.

### 8. Doctor Warning Mode During Runtime Update

- **Priority:** P1
- **Area:** update
- **Problem statement:** A successful runtime update can look failed because a post-upgrade doctor job fails and leaves noisy resources.
- **Proposed solution:** Treat doctor failure after successful Kubernetes upgrade as warning by default for `agentlab update`, unless configured as a hard gate. Keep real upgrade failures blocking.
- **Expected files likely to change:** `agentlab/update_cli.py`, `agentlab/release_upgrade.py`, `agentlab/k8s_operator.py`, `tests/test_update_cli.py`, `tests/test_release_upgrade.py`, `docs/release-upgrade.md`.
- **Test plan:** Verify doctor warning does not mark deployment failed, warning is visible, cleanup removes doctor leftovers, and upgrade failures remain hard failures.
- **Risk level:** Medium.
- **Why now / why later:** Useful soon, but do not mix it with update/release semantics changes.

### 9. Stronger Rust TestQuality Weak-Seam Reporting

- **Priority:** P2
- **Area:** tests
- **Problem statement:** Weak public seams are now warnings, but the MR feedback can still be more actionable.
- **Proposed solution:** Add clearer warning text for static-string-only seams and recommend existing route/config/error behavior. Keep warnings non-blocking when tasks explicitly allow public seam creation.
- **Expected files likely to change:** `agentlab/agents/test_quality.py`, `agentlab/policies/policy_engine.py`, `agentlab/services/mr_finalizer.py`, `tests/test_test_quality.py`, `tests/test_mr_finalizer.py`.
- **Test plan:** Keep assert-true/arithmetic blocking, verify app-name-only seam warning, and verify health route behavior passes cleanly.
- **Risk level:** Low.
- **Why now / why later:** Later because the core warning path already exists. Improve messaging after seeing real MR comments.

### 10. Review-Comment Command UX Polish

- **Priority:** P2
- **Area:** review-comments
- **Problem statement:** `/agent merge-status` exists, but command responses could become a broader operator toolkit.
- **Proposed solution:** Improve missing-artifact guidance, show exact artifact commands, and ensure unauthorized command responses remain concise. Consider a read-only `/agent artifacts` after observing needs.
- **Expected files likely to change:** `agentlab/scheduler.py`, `agentlab/review_comments.py`, `tests/test_review_comments.py`, `docs/scheduler.md`.
- **Test plan:** Cover missing artifacts, blocked gate, unauthorized user, and merge-ready except auto-merge disabled.
- **Risk level:** Low.
- **Why now / why later:** Later because merge-status is already available. Let operators use it before expanding command surface.

### 11. Recommended Operator Mode Documentation

- **Priority:** P2
- **Area:** docs
- **Problem statement:** The safe operating mode is clear in practice but spread across docs.
- **Proposed solution:** Add a short "Recommended Kubernetes Operator Mode" section that says: watch enabled, review-comments enabled, plan/action manual, action by explicit task ID, update via `agentlab update`.
- **Expected files likely to change:** `docs/scheduler.md`, `docs/kubernetes-operator-cli.md`, `docs/release-upgrade.md`.
- **Test plan:** Docs-only `git diff --check`.
- **Risk level:** Low.
- **Why now / why later:** Good companion PR after UX changes so docs match the improved commands.

### 12. State Cleanup UX

- **Priority:** P3
- **Area:** UX
- **Problem statement:** Release/update state can confuse operators after manual recovery.
- **Proposed solution:** Improve `agentlab update --clear-state` and release state display to show target image/version, completed state, and safe cleanup recommendations.
- **Expected files likely to change:** `agentlab/update_cli.py`, `agentlab/release_cli.py`, `agentlab/release_upgrade.py`, `tests/test_update_cli.py`, `tests/test_release_upgrade.py`, `docs/release-upgrade.md`.
- **Test plan:** Cover completed-state clear, incomplete-state refusal, and dry-run state display.
- **Risk level:** Low.
- **Why now / why later:** Later because normal update already has a workable state flow.

## Recommended Next 3 PRs

### PR 1: Merge-Aware Planner Deduplication

Scope:

- Store merged Agent MR metadata separately from closed-failed feedback.
- Add task fingerprints.
- Suppress duplicate merged docs tasks.
- Keep closed failed Rust feedback driving Rust follow-up planning.

Why first: This directly reduces repeated low-value tasks and improves manual planning signal quality.

### PR 2: Plan Preview Artifact And Manual Action Diagnostics

Scope:

- Add `plan_preview.md`.
- Print selected task summary and copy-paste action command.
- Improve `--task-id` missing/stale errors.

Why second: Once planner selection is better, operators need a clear pre-action review surface.

### PR 3: Compact Ops Status Command

Scope:

- Add `agentlab k8s ops-status`.
- Show runtime, failed/active jobs, scheduler state, token availability, and recommendation.
- Keep `k8s status` behavior unchanged.

Why third: This gives a daily operational command without changing scheduler behavior or update semantics.

## Things To Avoid Right Now

- Do not rework update/release semantics again.
- Do not auto-enable plan or action.
- Do not weaken TestQualityAgent placeholder blocking.
- Do not broaden allowed paths to `rust-backend/src/**`.
- Do not add Docker socket access.
- Do not create fake Rust tests.
- Do not treat merged Agent MRs as negative feedback.
- Do not slow review-comments by default if operators expect quick `/agent ...` responses.
- Do not combine Kubernetes runtime changes with planner behavior changes.
- Do not change generated deployment manifests in planning-only or docs-only tasks.

## Operator Workflow Recommendations

### Runtime Update

```bash
agentlab update --dry-run
agentlab update
```

If an update fails after a deploy step:

```bash
agentlab update --resume --dry-run
agentlab update --resume
```

### Status

Current full status:

```bash
agentlab k8s status --namespace agentlab --manifest-dir deploy/kubernetes/generated
agentlab k8s health --namespace agentlab --manifest-dir deploy/kubernetes/generated
```

After the proposed ops-status PR:

```bash
agentlab k8s ops-status --namespace agentlab --manifest-dir deploy/kubernetes/generated
```

### Planning With Focus

```bash
agentlab k8s run plan --focus "rust smoke test" --namespace agentlab --manifest-dir deploy/kubernetes/generated
agentlab k8s artifact latest approved_plan.json --namespace agentlab
```

After the proposed plan-preview PR:

```bash
agentlab k8s artifact latest plan_preview.md --namespace agentlab
```

### Manual Action

```bash
agentlab k8s run action --task-id <approved-task-id> --namespace agentlab --manifest-dir deploy/kubernetes/generated
```

Keep action paused as a CronJob. Prefer one explicit action run per reviewed plan.

### Cleanup

```bash
agentlab k8s cleanup-failed --dry-run --namespace agentlab
agentlab k8s cleanup-failed --yes --namespace agentlab
```

Use cleanup after verifying failed job/pod details, especially doctor leftovers.

### Review-Comment Operation

Keep review-comments enabled when operators expect quick MR responses:

```text
/agent status
/agent merge-status
/agent revise --dry-run
/agent apply
/agent stop
/agent resume
```

Keep plan and action manual until planner dedupe, plan preview, and action diagnostics are in place.

## Open Questions

1. Should `agentlab k8s ops-status` be separate from `agentlab k8s health`, or should health gain a compact mode?
2. What exact merged-MR lookback window should planner dedupe use: count-based, age-based, or both?
3. Should Cargo cache be enabled by default for the `small` resource profile, or opt-in only?
4. Should doctor warning mode be configurable globally, per update run, or both?
5. What is the safest default for review-comments successful job history on clusters that run it every minute?
6. Should `/agent merge-status` include exact artifact names and run IDs, or stay concise unless artifacts are missing?
7. Should action allow a single approved task without `--task-id`, or should explicit task IDs be required for all manual action runs?
8. Should completed update/release state be automatically cleaned, or retained for audit until an operator clears it?
