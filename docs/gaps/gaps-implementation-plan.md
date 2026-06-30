# Gaps — Implementation Plan

Consolidated plan for implementation-worthy gaps identified by architecture reviews. Doc-only gaps are tracked in the review files and addressed in ARCHITECTURE.md directly.

---

## Ephemeral Execution (source: `ephemeral-execution-gaps.md`)

### T1: Forward PermissionScope to sandbox contract (Gaps 3, 5)

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

### T2: Explicit sandbox termination on timeout/cancellation (Gap 4)

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

### T3: Cleanup failure metrics (Gap 6)

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

### Dependencies

```
T1 (permissions) — independent, but sandbox-side enforcement depends on upstream
T2 (timeout/cancel) — independent
T3 (cleanup metrics) — independent
```

All three are independent and can be parallelized.

### Decision points before implementation

1. **T1**: Does the sandbox `/v1/agent/run` contract already support `allowedTools`/`deniedTools`? If not, is this a lightspeed-cloud-agents task or an upstream sandbox task?
2. **T2**: Is Temporal heartbeat sufficient, or do we need an explicit container kill signal (e.g. `spawner.terminate()` that sends SIGTERM before destroy)?
3. **T3**: Should cleanup failure trigger an alert, or just a metric? (Metric first, alerting rule later.)

---

## Sandbox Runtime (source: `sandbox-runtime-gaps.md`)

### T4: Unify runtime HTTP contract (Gap 1)

**Problem**: The workflow path calls `POST /v1/agent/run` with `{query, context, systemPrompt, outputSchema}`. The in-repo generic runtime serves `POST /v1/run` with `{prompt, context}`. Two different routes, two different request schemas, no contract test to catch drift.

**What to build**:
1. Choose one canonical contract. The sandbox path (`/v1/agent/run`) is the production contract used by the Temporal workflow engine. The generic runtime (`/v1/run`) is an older path used for standalone agent deployments.
2. Add a `/v1/agent/run` route to the generic runtime that accepts the same request shape as the sandbox (`query`, `context`, `systemPrompt`, `outputSchema`). Map `query` → `prompt` internally. Keep `/v1/run` as a backward-compatible alias.
3. Add a contract test that validates both the workflow activity's request body and the generic runtime's accepted schema against a shared contract definition.

**Files**:
- `src/cloud_agents/runtime/server.py` — add `/v1/agent/run` route
- `src/cloud_agents/models.py` — add `AgentRunRequestV2` model matching sandbox contract (query, context, systemPrompt, outputSchema)
- `tests/unit/` — contract test asserting workflow activity and runtime accept same fields

**Effort**: 1-2 days

### T5: Document runtime input completeness (Gaps 2, 3, 5)

**Problem**: The ARCHITECTURE.md Sandbox Runtime section only mentions provider, model, credentials, and skills. It omits MCP server config, additional provider env vars (`LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, etc.), and the `credentials_secret` description says "API key" when it can be broader credential material.

**What to build**: Doc-only. Update the Sandbox Runtime section in ARCHITECTURE.md:
- Add MCP server injection (`LIGHTSPEED_MCP_SERVERS` env var) to the config table
- Add deployment-specific provider env vars to the table
- Change "API key" to "provider credentials"
- Scope the section explicitly to the workflow-spawned sandbox path

**Effort**: Half day

### T6: Runtime convergence — RESOLVED

**Decision**: Option 3 (remove). Generic runtime was PoC1 legacy — removed entirely. The sandbox `/v1/agent/run` is the only runtime contract. No convergence needed.

### ~~T4: Unify runtime HTTP contract~~ — CLOSED

No longer needed. The generic runtime was removed. Only one contract exists: `POST /v1/agent/run`.

### Dependencies

```
T5 (doc inputs) — independent, ready to implement
```

### Decision points before implementation

4. **T5**: Should the config table be exhaustive (every env var) or just the core ones?
