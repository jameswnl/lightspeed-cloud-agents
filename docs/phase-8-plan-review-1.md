# Review: phase-8-tasks.md

## Findings

### 1. Blocker (Functionality): Task 1 drops the durable attempt identity required for async retries
Task 1 changes the result-ingest contract to `POST /v1/workflows/{workflow_id}/steps/{step_name}/result`, but the earlier async design in `phase-6-tasks.md` required immutable `step_id` and `attempt_id` so the runner can reject duplicate callbacks, reject stale completions after a retry, and keep terminal attempts immutable.

Why it matters:
- `workflow_id + step_name` is not enough to distinguish attempt N from attempt N+1
- a late callback from a retried pod can overwrite or race the current attempt
- duplicate completions cannot be safely identified once retries exist

Recommended fix:
Keep the ingest API attempt-specific. Either preserve the Phase 6 `step_id` endpoint shape or require `attempt_id` as part of the callback contract and persisted step state. The task should explicitly say duplicate completions are ignored, stale attempt completions are rejected, and terminal attempt records are immutable.

### 2. Blocker (Functionality): Task 5 promises poll-based recovery without defining any durable recovery handle
Task 5 says the recovery poller will "attempt result recovery (poll agent pod) before marking orphaned steps as failed," but the plan never defines what persisted data lets another replica do that after a crash. The current task list does not say the runner stores a recoverable async run handle, a stable endpoint, or a pod/service identity that remains queryable after dispatch returns.

Why it matters:
- the poller cannot recover a result unless the runner persisted exactly what to poll
- if the original runner crashes after spawn, another replica needs a durable handle to the same async run
- if the pod exits quickly after a failed callback, the fallback path disappears unless the runtime keeps the result available long enough

Recommended fix:
Task 2 and Task 5 should define the persisted recovery contract explicitly: resource identity, reachable endpoint or service name, async `run_id` or equivalent poll handle, and the retention/lifecycle rule that keeps results recoverable until the runner acknowledges them.

### 3. High (Functionality): Task 3 omits the optimistic-locking contract that makes multi-replica advancement safe
Task 3 introduces `advance_workflow()` as the engine that reacts to ingested results and dispatches the next step, but it does not restate the CAS/optimistic-locking contract that the architecture and Phase 6 relied on to prevent duplicate advancement across replicas.

Why it matters:
- callback receiver and recovery poller can both try to advance the same workflow
- duplicate callbacks can race each other
- failover during result ingestion can cause two replicas to believe they should dispatch the successor step

Recommended fix:
Make Task 3 explicitly require workflow-version CAS and first-writer-wins attempt claiming before advancement. The plan should define how callback-vs-callback and callback-vs-poller races are resolved so successor steps are never double-dispatched.

### 4. High (Security): Task 10 cannot stay "independent" if async callback ingestion is the primary Kubernetes completion path
Task 10 moves Kubernetes auth to per-pod identity via TokenReview, but the dependency graph still marks it as independent and lowest priority. That is too weak for the new trust boundary introduced by Tasks 1 and 4, where ephemeral pods can now POST authoritative step completions into the trusted runner.

Why it matters:
- on Kubernetes, shared bearer-token auth means any pod with the shared token can forge another step's completion
- the new callback path is now part of the core workflow control plane, not just a convenience API
- the weaker Task 1 endpoint shape (`workflow_id + step_name`) increases the blast radius if caller identity is not bound to the specific spawned attempt

Recommended fix:
Either make Task 10 a prerequisite for Kubernetes production rollout of async callbacks, or clearly scope Phase 8 so callback mode is production-ready first for Podman/shared-secret deployments while Kubernetes remains blocked on TokenReview. The plan should also say how the ingest endpoint binds the caller identity to the specific spawned Job/ServiceAccount for that attempt.

### 5. Medium (Quality): Task 7 and Task 9 under-specify the reconstructibility and failure-path testing this phase depends on
Task 7 says to handle `AlreadyExists` on Job creation, but it no longer restates the reconstructible naming rule from `phase-7-tasks.md` that makes retry idempotency and crash cleanup actually work. Task 9 also only names replica failover at a high level and does not require coverage for the hardest async state transitions.

Why it matters:
- `AlreadyExists` handling alone is not enough unless the retry computes the same resource name from persisted inputs
- crash recovery and cleanup still depend on names being reconstructible from workflow state
- a capstone E2E that only covers callback success + replica failover can still miss duplicate callback, stale callback, lost callback, and post-persist/pre-advance crash boundaries

Recommended fix:
Restore the reconstructible naming requirement inside Task 7, including which persisted fields participate in the name. Expand Task 9 into concrete failure-path checks: duplicate callback, stale callback after retry, lost callback recovered by poller, crash after result persistence but before next-step dispatch, and cleanup after recovered completion.

## Perspective Check
- Functionality: remaining gaps around attempt identity, durable recovery handles, and safe multi-replica advancement.
- Quality: remaining gaps around dependency clarity, reconstructible idempotency, and concrete verification criteria for async failure modes.
- Security: remaining gaps because the callback-ingest trust boundary is not explicit enough for Kubernetes unless TokenReview is mandatory or clearly deferred out of production scope.

## Open Questions / Assumptions

- Assumed Phase 8 is intended to preserve the Phase 6 async state-machine contracts rather than replace them with a simpler callback model.
- Assumed Task 5 intends real post-crash result recovery, not only best-effort polling while the original pod happens to still be alive.
- Assumed both Kubernetes and Podman remain first-class targets for this phase, consistent with `ARCHITECTURE.md`.

## Summary

The phase direction is right, but the current task doc is missing key runtime contracts that make async dispatch safe in production. The biggest gaps are attempt identity, durable recovery semantics, multi-replica advancement rules, and the Kubernetes trust boundary for callback ingestion.
