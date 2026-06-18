# Review: Phase 1b Implementation

## Findings

### 1. High: async runs that fail at the agent layer are recorded as completed successes

`run_monitoring()` and `run_diagnostic()` return `AgentRunResponse(success=False, ...)` on internal failure instead of raising. But the async runtime path treats any returned envelope as success:

- increments the `"success"` metric
- stores the run as `COMPLETED`
- does not convert `success=False` into `FAILED`

#### Why this matters

This means:

- `GET /v1/runs/{id}` can report `status="completed"` for a failed run
- metrics overcount successful runs
- async callers get misleading terminal state

This is a real runtime bug, not just a metrics inconsistency.

#### Recommendation

In the async path, check `result.success` before marking completion:

- `success=True` → `complete_run(...)`
- `success=False` → `fail_run(...)` and increment error/failure metrics

The async state machine should reflect application-level failure, not just exceptions.

### 2. High: correlation ID sanitization only reaches the response header, not the agent-level log path

The server validates the correlation ID, but it does not write the sanitized value back into `body.context`. The monitoring and diagnostic runners then read the raw value from `request.context` and log it directly.

#### Why this matters

That defeats the stated log-injection hardening:

- the HTTP response header is sanitized
- the actual agent log entries can still use unsanitized caller input

So the security control is only half-implemented.

#### Recommendation

After validation, normalize the request context before invoking the runner. The runner should see the same validated `correlation_id` that the server returns in `X-Correlation-ID`.

### 3. Major: the redispatch-prevention fix does not fully remove the anomaly signals that triggered dispatch

`MonitoringLoop._mark_hosts_healthy()` only flips `host["status"] = "healthy"`. But the same host still keeps its degraded metrics and crashed service state.

#### Why this matters

In the `bad_deploy` scenario, the monitoring tool still sees:

- `cpu = 92`
- `memory = 88`
- `services["app"] = "crashed"`

So even after local state mutation, the monitoring agent still has enough evidence to raise alerts again on later iterations.

The existing test suite misses this because the “second cycle” test mocks the monitoring runner to return healthy output rather than re-evaluating real mutated state.

#### Recommendation

Make local post-dispatch mutation consistent with the resolved issue:

- either fully mutate the affected host back to a healthy baseline
- or store a suppression marker/TTL so subsequent cycles intentionally skip re-alerting

Right now the loop only fixes one field while leaving the rest of the anomaly intact.

### 4. Major: the advertised `dispatched_run_ids` verification path still is not implemented

`MonitoringResult` includes `dispatched_run_ids`, but there is no write path in the implementation that populates this field. The monitoring loop dispatches to the diagnostic agent, but it uses the synchronous `run()` method and never captures async run IDs.

The shipped E2E scenarios also do not include the promised monitoring→diagnostic dispatch verification or full cross-pod remediation flow.

#### Why this matters

This creates a doc/code/test mismatch:

- the model suggests dispatch IDs are part of the contract
- the implementation never sets them
- the E2E suite does not prove the cross-pod flow that the design claimed

That means the most important multi-agent verification path is still absent in practice.

#### Recommendation

Decide which behavior is real for Phase 1b:

- if monitoring dispatch is async and verifiable, use `run_async()` and populate `dispatched_run_ids`
- if not, remove or defer the field and the verification claim

The current state keeps the artifact of the design without the behavior.

## Verification

I ran:

```bash
uv run pytest tests/unit/agents -q
```

Result:

- **147 passed**

## Summary

The Phase 1b implementation is substantial and mostly well-structured. The async runtime, run store, monitoring agent, and correlation helpers are all in place, and the targeted unit suite passes cleanly.

The biggest remaining problems are at the seams:

- async run state semantics are wrong on logical failure
- correlation ID sanitization does not reach the agent loggers
- redispatch prevention does not actually clear the underlying anomaly signals
- the promised dispatch-verification path still is not implemented in code/tests

These are fixable, but they are meaningful behavioral issues rather than polish.
