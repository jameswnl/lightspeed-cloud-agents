# Review: phase-6-tasks.md

## Findings

### 1. Blocker: The architecture promises stateless resumable event streaming, but no task actually implements it
The problem statement says the current in-memory SSE queue must be replaced with persisted events and client resume via `last_event_id`, but the task breakdown never includes an event store, event schema, retention policy, replay endpoint behavior, or API changes for resumed streams.

This matters because stateless multi-replica routing breaks if streaming still depends on in-memory queues. This is one of the explicit “current problems,” but there is no implementable mechanism in the plan to solve it.

Recommended fix: either add a dedicated task for persisted workflow events plus `last_event_id` replay semantics, or explicitly defer resumable SSE to a later phase and remove it from the “after” architecture claims.

### 2. Blocker: Direct DB writes from ephemeral pods collapse the trust boundary
Task 4 says the spawned pod gets `WORKFLOW_POSTGRES_URL` and writes results directly to the workflow database. That gives every ephemeral agent pod database-level write authority over workflow state, and likely over workflow definitions too unless the schema or credentials are split.

This matters because this is a major security regression in a phase whose main goal is stateless production scaling. A compromised or buggy agent pod could mutate arbitrary workflow rows, fake completions, or corrupt definitions. The plan does not define scoped DB credentials, row-level constraints, or a narrower write surface.

Recommended fix: do not give spawned pods broad workflow DB credentials. Prefer a dedicated result-ingest API with auth, or at minimum use scoped credentials restricted to append/update of that step’s result rows only.

### 3. Major: The workflow-definition lifecycle is under-specified for active runs
Phase 6 replaces single-file workflow loading with stored definitions and `run by name`, but the plan never says whether runs bind to an immutable snapshot/version of a definition. It also allows delete-by-name without defining what happens to in-flight runs created from that definition.

This matters because in a stateless system, retries, approval resumes, and recovery polling all depend on a stable workflow contract. If a definition is edited or deleted after a run starts, different replicas may interpret the same run differently.

Recommended fix: make runs reference an immutable stored definition version or snapshot at submission time, and define whether delete is blocked for referenced definitions or only removes future availability.

### 4. Major: The async dispatch state machine is missing a durable idempotency contract
The plan introduces `step_id`, callback-triggered advancement, optimistic locking, retries, and a poller for orphaned dispatched steps, but it never defines the durable state machine around step attempts. There is no explicit contract for duplicate callbacks, stale pod completions after retry, or how a retried step distinguishes attempt N from attempt N-1.

This matters because with multiple replicas and both callback and poller advancing the same run, idempotency is the difference between “stateless” and “racey.” A single `version` field on the workflow row is not enough unless step-attempt identity and completion semantics are also explicit.

Recommended fix: define a per-dispatch attempt record with immutable `step_id`/`attempt_id`, terminal-state transition rules, and explicit handling for duplicate or stale completions.

### 5. Major: The verification plan is too weak for the behaviors this phase introduces
The phase introduces multi-replica advancement, DB-backed definition storage, callback + poller recovery, optimistic locking, and stateless resume. But the verification section only lists broad unit/example pytest commands plus one happy-path 2-replica E2E outline. It does not require tests for lost callbacks, duplicate callbacks, CAS conflicts, definition mutation during active runs, or cross-replica approval resume after restart.

This matters because these are exactly the seam and concurrency bugs most likely to break the design in production. The current acceptance path could pass while missing the hardest failure modes.

Recommended fix: add explicit verification cases for callback loss, duplicate completion notifications, optimistic-lock conflicts, approval resume on a different replica, and definition update/delete while runs are active.

## Perspective Check
- Functionality: remaining gaps around persisted event replay, immutable definition binding, and async step-attempt semantics.
- Quality: task breakdown is directionally coherent, but the verification plan does not yet cover the highest-risk stateless/concurrency behaviors.
- Security: major trust-boundary gap remains around giving spawned pods direct workflow DB write access.

## Open Questions / Assumptions
- Are workflow definitions meant to be immutable-by-version once referenced by a run?
- Is resumable SSE part of Phase 6 scope, or was it accidentally left in the architecture section without a matching task?
- Should spawned agent pods ever write directly to the workflow DB, or should they only notify a trusted runner/API?
- Is `step_id` intended to identify a logical step or a specific dispatch attempt?

## Summary
The Phase 6 direction makes sense and follows naturally from the stateful limits exposed in earlier phases, but the plan is not fully closed yet. The two biggest problems are the missing implementation path for persisted/resumable events and the overly broad DB trust granted to spawned pods. If those contracts are tightened, the rest of the stateless-runner design becomes much more credible.
