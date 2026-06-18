# Review: Phase 1b Fix Commit

## Findings

### 1. Major: synchronous `/v1/run` still treats `success=False` envelopes as successful runs

The async path was fixed so that returned `AgentRunResponse(success=False, ...)` values are stored as failed runs and counted as errors. However, the synchronous path still increments the `"success"` metric and returns HTTP 200 for any returned `AgentRunResponse`, even when the runner explicitly signaled failure.

#### Why this matters

This leaves sync and async behavior inconsistent:

- **async**: application-level failure becomes failed run state
- **sync**: application-level failure still looks like success at the HTTP/metrics layer

That means the fix landed only for one execution mode, not for the full runtime contract.

#### Recommendation

Make sync semantics match async semantics. If the runner returns `success=False`, the server should not count it as a successful run.

### 2. Major: the `dispatched_run_ids` verification path still is not implemented in the public monitoring API

`MonitoringResult` includes `dispatched_run_ids`, and `MonitoringLoop._check_and_dispatch()` now returns a list of run IDs. But `run_monitoring()` does not call the loop or populate that field, so `POST /v1/run` to the monitoring agent still cannot return dispatch IDs.

The E2E suite also still does not contain the monitoring → diagnostic dispatch/full-flow scenarios that the design had described.

#### Why this matters

This keeps a mismatch between:

- the model contract
- the internal loop behavior
- the public runtime behavior
- the E2E coverage story

In other words, the field exists and the loop has internal IDs, but the externally claimed dispatch-verification path is still not wired through.

#### Recommendation

Either:

- wire monitoring `/v1/run` to expose `dispatched_run_ids`, or
- explicitly defer that behavior and remove the claim from the contract for now

### 3. Medium: redispatch prevention is still specialized to service/cpu-style incidents, not all alert types

`_mark_hosts_healthy()` now resets status, CPU, memory, and services, which fixes the earlier degraded-service case. But it still does not touch disk state.

#### Why this matters

The monitoring agent’s alert model includes disk-based alerts. A host that triggered dispatch because of high disk usage can still remain above threshold after the local mutation step and therefore re-alert on a later cycle.

The current tests do not cover this because the redispatch-prevention test still mocks the second monitoring result rather than re-evaluating real mutated state for different alert classes.

#### Recommendation

Either:

- broaden the local state repair logic to cover all alert classes you want to suppress, or
- introduce an explicit suppression marker/TTL instead of trying to infer “fixed” from partial local mutation

## What Improved

Two important fixes did land correctly:

1. Async runs with `success=False` now become `FAILED`, not `COMPLETED`
2. Sanitized correlation IDs are now written back into `request.context`, so the runner log path sees the validated ID rather than the raw caller value

Those are meaningful improvements and close two important seam-level issues from the prior review.

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/runtime/test_server.py tests/unit/agents/monitoring/test_loop.py tests/unit/agents/test_remote_agent_client.py -q
```

Result:

- **42 passed**

## Summary

The fix commit materially improves the Phase 1b implementation, but I would not consider every previous finding fully closed yet.

The remaining gaps are:

- sync failure semantics still differ from async
- `dispatched_run_ids` still exists more in model/design than in public runtime behavior
- redispatch prevention is still only partially generalized

So this is a real improvement, but not a full close-out of the entire prior review.
