# Review: Phase 1b Plan

## Findings

### 1. Blocker: the chosen shared-state approach still leaves monitoring and diagnostic with independent mutable worlds

The plan avoids a shared state service and instead relies on both pods pre-seeding the same scenario plus passing alert context over HTTP. That sounds clean, but it means remediation performed by the diagnostic pod does not update the monitoring pod's local state.

#### Why this matters

With the current design:

- monitoring sees a degraded scenario
- monitoring dispatches diagnostic
- diagnostic remediates its own local copy of state
- monitoring wakes up again and still sees the original degraded scenario

That creates repeated redispatch unless you add explicit suppression or trust semantics.

This is the biggest mismatch between the proposed implementation model and the claimed "full cross-pod remediation flow."

#### Recommendation

Choose and document one of these explicitly:

- **Option A:** Monitoring trusts a successful diagnostic result and suppresses redispatch for a TTL/window.
- **Option B:** Monitoring mutates its own local scenario state after a successful diagnostic result.
- **Option C:** Use a minimal shared state mechanism after all.

Right now the plan describes realistic alert-context passing, but not a complete state model for repeated periodic checks.

### 2. Major: the `/livez` design is likely to create false failures for the monitoring agent

The plan says heartbeat is updated on run start and completion, and `/livez` returns 503 if the heartbeat gets too old. But the monitoring agent is a background loop that may be healthy while idle between intervals.

#### Why this matters

If liveness only reflects request activity:

- a healthy monitoring pod can look dead
- long monitoring intervals can trigger spurious restarts
- the signal measures endpoint usage, not loop health

This is especially risky because the monitoring agent's most important behavior is autonomous background work, not request handling.

#### Recommendation

Separate the concepts:

- **readiness**: can accept work
- **liveness**: loop/process is still progressing

For the monitoring pod, update heartbeat from the loop itself, not just `/v1/run` lifecycle. Otherwise `/livez` is measuring the wrong thing.

### 3. Major: per-tool Prometheus metrics are promised, but there is no instrumentation point in the task plan

The plan commits to `agent_tool_calls_total{agent_name, tool_name}`, but the implementation tasks only mention adding a metrics module and instrumenting `server.py`.

#### Why this matters

Server-level instrumentation can count:

- runs
- durations
- statuses

But it cannot know which tool names were called unless there is an explicit hook at the tool boundary.

Without a defined instrumentation point, this metric is underspecified and at high risk of becoming:

- fake coverage in tests
- under-implemented in code
- silently omitted while the rest of the task appears complete

#### Recommendation

Add an explicit design note for where tool-call metrics are recorded, for example:

- wrapper around `agent.tool_plain(...)`
- instrumentation inside each tool function
- a Pydantic AI callback/hook if available

If you do not want to define that now, defer per-tool metrics and keep only per-run metrics in Phase 1b.

### 4. Major: the E2E verification model for monitoring-triggered dispatch is still too vague

The plan adds `dispatched_run_ids` to `MonitoringResult`, but the background monitoring loop is not yet described as exposing that result in a deterministic way that E2E tests can fetch. At the same time, the current E2E harness is built around a single base URL and direct HTTP calls to one agent.

#### Why this matters

Phase 1b is not just "4 more scenarios." It needs a new multi-agent test harness that can:

- talk to both monitoring and diagnostic agents
- observe async run IDs
- verify dispatch happened
- verify the diagnostic side completed

Without a concrete observation path, the dispatch scenarios risk becoming log-scraping or hand-wavy assertions.

#### Recommendation

Define the E2E observation contract up front. For example:

- monitoring `/v1/run` returns `MonitoringResult` including `dispatched_run_ids`
- test polls diagnostic `/v1/runs/{id}` using those IDs
- both agents have separate configurable E2E base URLs

That makes the dispatch proof deterministic instead of incidental.

## Open Questions / Assumptions

1. Should the monitoring loop suppress redispatch after a successful diagnostic result, even if its own local simulated state remains degraded?
2. Is `/livez` intended to represent endpoint activity only, or actual background loop health?
3. Are per-tool metrics truly required in Phase 1b, or would per-run metrics be enough for this phase?
4. What is the exact E2E observation path for proving monitoring -> diagnostic dispatch across pods?

## Summary

The Phase 1b plan is stronger than the earlier docs because it resolves several real architectural questions early, especially around async submit/poll and the decision to keep observability lightweight.

The biggest remaining issue is that the plan still combines:

- pod-local simulated mutable state
- periodic autonomous monitoring
- cross-pod remediation claims

without fully explaining how those three interact over time.

If you tighten one thing first, tighten the state model and the E2E observation model. Those two decisions will determine whether Phase 1b feels like a clean extension of Phase 1a or a stack of compensating workarounds around fake state.
