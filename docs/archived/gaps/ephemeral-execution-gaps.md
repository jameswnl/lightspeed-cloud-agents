# Ephemeral Execution Gaps

This note captures the code-to-doc gaps in the `### Ephemeral-by-default execution` section of `docs/ARCHITECTURE.md`.

Goal: separate what is already true in code from what is only partly true, overstated, or not implemented yet so we can decide what to fix in code versus what to reword in docs.

## Section Reviewed

Current architecture claims:

- Every workflow step spawns a fresh container
- Starts clean with no state from previous steps
- Has only the tools configured for this agent type
- Has hard timeouts and is killed automatically if it hangs
- Has scoped permissions limited to what the workflow author declares
- Is destroyed after execution
- All steps currently use ephemeral spawning
- Pre-deployed agents are a backlog item and `spawn` is not implemented

## What The Code Clearly Supports

### 1. Agent steps currently use spawned sandbox execution

The workflow engine always dispatches `agent` steps to `run_sandbox_step`, and that activity always calls `spawner.spawn(...)`.

Implication:
- Current execution is effectively ephemeral for `agent` steps.
- The `spawn` field exists in the schema but is not consulted by the execution path.

Code paths:
- `src/cloud_agents/workflow/temporal_workflow.py`
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/workflow/definition.py`

### 2. Prior step data is passed as request context, not shared container state

The sandbox activity builds a request context from prior workflow results and sends it with the HTTP request.

Implication:
- Container-local state is isolated per run.
- Prior workflow outputs are still intentionally visible to later steps through explicit context passing.

Code paths:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/workflow/temporal_context.py`

### 3. Best-effort cleanup exists

The sandbox activity destroys the spawned runtime in a `finally` block, and startup orphan reconciliation exists as a recovery mechanism.

Implication:
- Cleanup is intended.
- Cleanup is not guaranteed, which is why orphan cleanup exists.

Code paths:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/workflow/temporal_entrypoint.py`

## Gaps Between Doc And Code

### Gap 1. "Every workflow step spawns a fresh container"

Status: partially true

Why:
- `agent` steps spawn a runtime.
- `human-approval` steps do not spawn anything.
- Retry/idempotency behavior may reuse an existing same-named spawned resource rather than guaranteeing a completely new one every time.

Evidence:
- `src/cloud_agents/workflow/temporal_workflow.py` branches by step type.
- `src/cloud_agents/workflow/temporal_activities.py` derives deterministic pod names from `(workflow_id, step_name, attempt)`.
- Spawners handle idempotent existing resources.

Decision to make:
- Do we want the doc to say "every agent step" instead of "every workflow step"?
- Do we want strict fresh-instance semantics on retry, or is idempotent reuse acceptable?

Possible implementation work:
- If strict freshness matters, remove same-name reuse behavior and force replacement semantics.
- If not, keep code as-is and tighten doc wording.

### Gap 2. "Starts clean — no state from previous steps"

Status: partly misleading

Why:
- There is no shared in-container runtime state across steps.
- But prior step outputs are explicitly injected into the request context for later steps.

Evidence:
- `build_sandbox_context(...)` assembles prior workflow outputs for downstream steps.

Decision to make:
- Is the architecture claim really about process isolation, or about no carry-over of workflow data?

Possible implementation work:
- No code change needed if the intent is process isolation.
- Reword doc to say "no shared container/process state" if that is the real guarantee.

### Gap 3. "Has only the tools configured for this agent type"

Status: not established by this workflow path

Why:
- The workflow engine spawns a sandbox image and sends prompt/context.
- This path does not prove that each workflow step gets a per-step tool set enforced by the workflow layer.
- Tool filtering exists in the generic runtime, but that is not currently driven here in a way that fully supports this sentence.

Evidence:
- `src/cloud_agents/runtime/generic_runner.py` supports advisory-mode tool filtering and permission-based filtering.
- `src/cloud_agents/workflow/temporal_activities.py` does not pass `allowed_tools` / `denied_tools` into the sandbox request context.

Decision to make:
- Should the workflow layer own per-step tool scoping?
- Or should the doc describe tool availability more generically?

Possible implementation work:
- Pass `allowed_tools` / `denied_tools` from workflow step permissions into sandbox context.
- Ensure the runtime contract consumes them consistently.
- Add tests that verify tools are actually filtered for workflow-driven runs.

### Gap 4. "Has hard timeouts — killed automatically if it hangs"

Status: overstated

Why:
- There is a Temporal activity timeout.
- There is an HTTP client timeout for the sandbox call.
- There is readiness polling timeout.
- But the code does not explicitly kill the spawned runtime because it hung mid-request.
- Cleanup is best-effort in `finally`, and orphan reconciliation exists because cleanup may be missed.

Evidence:
- `start_to_close_timeout` is set in `temporal_workflow.py`.
- HTTP timeout is derived in `temporal_activities.py`.
- No explicit timeout-driven destroy/terminate path exists separate from best-effort cleanup.

Decision to make:
- Do we want strong "killed automatically" semantics or just bounded workflow waiting semantics?

Possible implementation work:
- Add explicit cancellation/termination handling when request timeout or activity cancellation occurs.
- Align readiness timeout, HTTP timeout, and Temporal timeout semantics.
- Add tests for hung sandbox cleanup.

### Gap 5. "Has scoped permissions — only what the workflow author declares"

Status: overstated

Why:
- The current workflow path uses only a limited subset of step permissions.
- It applies `service_account` and timeout-related values.
- It does not currently pass workflow permission scopes through in a way that proves full enforcement of declared tool permissions.

Evidence:
- `src/cloud_agents/workflow/permissions.py` defines `allowed_tools` / `denied_tools`.
- `src/cloud_agents/runtime/generic_runner.py` can enforce those when present in request context.
- `src/cloud_agents/workflow/temporal_activities.py` does not currently forward them.

Decision to make:
- Should workflow permission declarations be authoritative runtime policy?

Possible implementation work:
- Forward `allowed_tools` / `denied_tools` into sandbox context.
- Add contract tests showing workflow permission scope actually changes available tools.

### Gap 6. "Is destroyed after execution — no cleanup worries"

Status: too strong

Why:
- Cleanup is attempted in `finally`.
- A failed destroy only logs a warning.
- Startup orphan reconciliation exists because resources can be left behind.

Evidence:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/workflow/temporal_entrypoint.py`

Decision to make:
- Is best-effort cleanup acceptable, or do we want stronger guarantees?

Possible implementation work:
- Add stronger cleanup retries and structured failure handling.
- Track cleanup failures as first-class workflow events/metrics.
- Keep orphan reconciliation as a safety net.

### Gap 7. "A stuck LLM call can't block the workflow runner"

Status: too absolute

Why:
- Timeouts bound waiting.
- But a hung step can still occupy worker capacity until timeout/cancellation.
- This is better than unbounded waiting, but it is not the same as "cannot block the runner."

Decision to make:
- Should the architecture promise bounded impact, or total non-blocking behavior?

Possible implementation work:
- Worker-level concurrency controls are already present through Temporal worker config.
- If stronger isolation is desired, document "bounded by activity timeout" instead of "can't block".

### Gap 8. "A misbehaving agent can't crash the platform"

Status: too absolute

Why:
- Isolation reduces blast radius.
- But the code does not prove an absolute crash-proof guarantee.

Decision to make:
- This is probably a documentation problem, not an implementation problem.

Possible implementation work:
- Rephrase to "reduces blast radius" or "isolates failures to the spawned runtime."

## Highest-Value Implementation Candidates

If we want to close code gaps instead of only rewording docs, these seem like the most concrete wins:

1. Implement workflow-to-runtime permission propagation
- Forward `allowed_tools` / `denied_tools` from workflow step permissions into sandbox request context.
- Verify runtime enforcement with focused tests.

2. Strengthen timeout and cleanup semantics
- Add explicit destroy/cancel handling for hung or timed-out sandbox requests.
- Make cleanup failure observable through workflow events and metrics.

3. Decide on retry freshness semantics
- Either guarantee new runtime instances on retry, or document idempotent reuse as intentional behavior.

## Recommended Doc Changes Even If We Implement Nothing

If we leave code unchanged, the section should say something closer to:

- Agent steps currently run in spawned sandbox runtimes.
- Each agent step gets isolated runtime state, while prior workflow outputs are passed explicitly in request context.
- Execution is bounded by workflow and request timeouts.
- Cleanup is best-effort, with orphan reconciliation as a recovery mechanism.
- The `spawn` field exists in the schema but the current execution path always uses spawned sandbox execution.

## Open Questions

1. Should workflow-declared permission scope be enforced as hard runtime policy?
2. Do we want strict fresh-instance guarantees on retries, or is deterministic reuse fine?
3. Is the target guarantee "process isolation" or "no prior workflow data visible"?
4. Should timeout semantics include explicit sandbox termination, or is best-effort cleanup enough?
