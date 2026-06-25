# Review: phase-8-tasks.md

## Findings

### 1. Blocker (Functionality): the plan still has no implementable recovery mechanism for "spawned but not yet persisted"
The updated doc now defines a strong recovery contract **after** a `"dispatched"` `StepResult` is persisted, but it still does not explain how the system recovers or cleans up work if the runner crashes after spawning the Job/container and before that state is durably written. This is now especially visible because Task 9 explicitly includes the scenario "kill replica after spawning pod but before persisting `dispatched` -> recovery poller detects and cleans up," but no prior task defines how the poller can detect orphaned work that has no persisted step record yet.

Why it matters:
- the recovery poller only knows about work that already exists in workflow state
- reconstructible naming only helps once the attempt identity is durably recorded
- the current Task 9 scenario claims an E2E guarantee the design still cannot implement

Recommended fix:
Add an explicit pre-persist crash-boundary mechanism. For example: persist an attempt record before spawn and make spawn idempotent from that record, or define a labels-based reconciliation scan that can discover unl inked spawned resources and map them back to workflow attempts. If that boundary is intentionally out of scope, remove or narrow Task 9 scenario 7 so the test plan matches the design.

### 2. High (Quality/Functionality): Task 5 still conflates "persist recovered result" with "advance workflow"
Task 5 says the poller should `poll_run(run_id)` and then "ingest result via `advance_workflow()` (with CAS, same as callback path)." But Task 3 defines `advance_workflow()` as the function that reacts **after** a step has already been completed in workflow state. It does not define a way to persist the recovered step result itself. Relatedly, the doc still has two competing descriptions of persistence ownership: the "Async Runtime Contracts" section says `dispatch_async()` persists the `"dispatched"` `StepResult`, while Task 2 says `dispatch_async()` returns a `StepResult`, implying the caller persists it.

Why it matters:
- the poller needs a defined state transition for "recovered result persisted" before advancement can be correct
- otherwise recovered completions may bypass the same idempotent ingest rules as callbacks
- mixed ownership of persistence makes the crash boundaries and CAS responsibilities hard to reason about

Recommended fix:
Split the contract into two explicit steps shared by both callback and poller paths:
1. `ingest_step_result(...)` persists the recovered/completed attempt with CAS and idempotency rules
2. `advance_workflow(...)` runs only after the step is durably terminal

Also make one component the clear owner of persisting the initial `"dispatched"` state: either `dispatch_async()` persists it internally, or it only returns a `StepResult` and the executor/dispatcher caller persists it. The doc should pick one and use it consistently.

## Perspective Check
- Functionality: remaining gaps. Post-persist recovery is much clearer now, but the pre-persist crash boundary and poller result-ingest path are still not fully implementable as written.
- Quality: remaining gaps. The test plan overclaims one crash-boundary scenario, and persistence ownership is still described inconsistently.
- Security: covered for this pass. No new major security issues found beyond the now-explicit Kubernetes TokenReview gate.

## Open Questions / Assumptions

- Assumed Task 9 scenario 7 is meant to be a real supported guarantee, not just an aspirational stress case.
- Assumed `advance_workflow()` is intended to run after the step result is already stored, consistent with Task 1's callback path.

## Summary

This revision closes most of the original review cleanly. The remaining issues are narrower now: one missing crash-boundary mechanism before persistence exists, and one remaining ambiguity in how recovered results are ingested versus advanced. Once those are resolved, this should be close to LGTM.
