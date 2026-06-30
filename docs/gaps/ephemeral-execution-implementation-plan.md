# Ephemeral Execution Gaps — Implementation Plan

**Date**: 2026-06-30
**Source**: `docs/gaps/ephemeral-execution-gaps.md` (reviewer gap analysis)
**Scope**: Three implementation-worthy gaps from the ephemeral execution review. Remaining gaps (retry freshness, blast radius claims) are doc-only — already addressed in ARCHITECTURE.md rewrite.

## T1: Forward PermissionScope to sandbox contract (Gaps 3, 5)

**Problem**: `allowed_tools` / `denied_tools` from `WorkflowStepSpec.permissions` are never passed to the sandbox. The model exists in `permissions.py`, enforcement exists in `generic_runner.py`, but the Temporal workflow path doesn't wire them through. Per-step tool scoping is a paper promise.

**What to build**:
1. In `temporal_activities.py` `_run_sandbox_step_inner()`, extract `allowed_tools` and `denied_tools` from `step.permissions` and include them in the `request_body` sent to the sandbox:
   ```python
   if permissions.get("allowed_tools"):
       request_body["allowedTools"] = permissions["allowed_tools"]
   if permissions.get("denied_tools"):
       request_body["deniedTools"] = permissions["denied_tools"]
   ```
2. The sandbox runtime must consume these fields. Check whether the `/v1/agent/run` contract in lightspeed-agentic-sandbox already accepts `allowedTools`/`deniedTools`, or whether this requires an upstream contract extension.
3. If the sandbox doesn't support it yet, this is a two-part task: activity-side forwarding (this repo) + sandbox-side enforcement (upstream).

**Files**:
- `src/cloud_agents/workflow/temporal_activities.py` — forward permissions to request body
- Upstream sandbox — consume `allowedTools`/`deniedTools` in the agent runner (TBD)

**Tests**:
- Unit test: `allowed_tools` in step permissions → appears in sandbox POST body
- Unit test: no permissions → no `allowedTools`/`deniedTools` in body
- Integration test (once sandbox supports it): workflow step with `denied_tools: ["run_remediation"]` → agent cannot call that tool

**Effort**: 1 day (activity side) + TBD (sandbox side)

## T2: Explicit sandbox termination on timeout/cancellation (Gap 4)

**Problem**: When a Temporal activity times out or is cancelled, the `finally` block attempts cleanup. But if the worker crashes between the timeout and the `finally`, the container leaks. There's no explicit cancellation signal to the spawned container — it just gets orphaned until startup reconciliation.

**What to build**:
1. Handle Temporal activity cancellation in `run_sandbox_step`. The Temporal SDK raises `asyncio.CancelledError` when an activity is cancelled. The current `finally` block already handles this, but doesn't distinguish between normal completion and cancellation.
2. Add a Temporal heartbeat in long-running sandbox calls. If the worker crashes, Temporal detects missing heartbeats and re-dispatches the activity to another worker, which runs orphan reconciliation on startup.
3. Add a metric for timeout-triggered destroys vs normal destroys so operators can see how often sandboxes are timing out.

**Files**:
- `src/cloud_agents/workflow/temporal_activities.py` — add `activity.heartbeat()` during sandbox HTTP call, distinguish cancellation in `finally`
- `src/cloud_agents/workflow/temporal_metrics.py` — add `ls_sandbox_timeout_total` counter

**Tests**:
- Unit test: activity cancellation → destroy still called
- Unit test: heartbeat called during sandbox HTTP request
- Unit test: timeout counter incremented on timeout-triggered destroy

**Effort**: 1 day

## T3: Cleanup failure metrics (Gap 6)

**Problem**: A failed `spawner.destroy()` only logs a warning. There's no metric, so leaked containers are invisible in dashboards until someone checks logs or orphan reconciliation runs.

**What to build**:
1. Add `ls_sandbox_cleanup_failures_total` Prometheus counter in `temporal_metrics.py`
2. Increment it in the `except` block of the `finally` cleanup in `temporal_activities.py`
3. Add `ls_sandbox_orphans_cleaned_total` counter for orphan reconciliation in `temporal_entrypoint.py`

**Files**:
- `src/cloud_agents/workflow/temporal_metrics.py` — add counters
- `src/cloud_agents/workflow/temporal_activities.py` — increment on cleanup failure
- `src/cloud_agents/workflow/temporal_entrypoint.py` — increment on orphan cleanup

**Tests**:
- Unit test: destroy failure → `ls_sandbox_cleanup_failures_total` incremented
- Unit test: orphan reconciliation → `ls_sandbox_orphans_cleaned_total` incremented

**Effort**: Half day

## Dependencies

```
T1 (permissions) — independent, but sandbox-side enforcement depends on upstream
T2 (timeout/cancel) — independent
T3 (cleanup metrics) — independent
```

All three are independent and can be parallelized.

## Decision points before implementation

1. **T1**: Does the sandbox `/v1/agent/run` contract already support `allowedTools`/`deniedTools`? If not, is this a lightspeed-cloud-agents task or an upstream sandbox task?
2. **T2**: Is Temporal heartbeat sufficient, or do we need an explicit container kill signal (e.g. `spawner.terminate()` that sends SIGTERM before destroy)?
3. **T3**: Should cleanup failure trigger an alert, or just a metric? (Metric first, alerting rule later.)
