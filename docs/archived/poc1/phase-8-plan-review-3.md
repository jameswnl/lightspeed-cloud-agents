# Review: phase-8-tasks.md

## Findings

### 1. High (Functionality): the `run_id=None` recovery path still lacks a defined re-dispatch outcome when no pod exists
The updated doc now correctly moves to a persist-before-spawn contract and defines the `run_id=None` state for crashes before async submission. But the concrete recovery behavior is still inconsistent across sections:

- the `Persist-Before-Spawn` section says that after "crash after persist, before spawn," the poller sees the `dispatched` step and "either re-spawns (idempotent via Task 7) or marks orphaned"
- Task 5 then defines the `run_id=None` path as: "if pod is reachable, re-submit async; if not, mark orphaned"

That still leaves no explicit mechanism for the main recovery case where the persisted step exists but the pod was never spawned or is already gone. Marking the step orphaned is not enough by itself unless the design also defines who converts that state into a retry/re-dispatch and under what CAS rules.

Why it matters:
- the whole point of persist-before-spawn is to survive crashes before submission
- if "no pod exists" only leads to "mark orphaned," the workflow can stall on a recoverable dispatch gap
- Task 9's recovery claims still depend on a concrete path from `dispatched + run_id=None + no pod` to a re-dispatched attempt or a well-defined terminal failure transition

Recommended fix:
Make the `run_id=None` recovery rule explicit and consistent in Task 5. For example:
- if `run_id is None` and pod is reachable: re-submit async, persist returned `run_id`
- if `run_id is None` and pod is not reachable: either re-spawn + re-submit the same attempt idempotently, or convert the step into a failed/orphaned terminal state and immediately route it through the normal retry policy in `advance_workflow()`

Whichever path you choose, document who performs it, when CAS is applied, and how it avoids duplicate submission if two replicas race.

## Perspective Check
- Functionality: remaining gap. Most async contracts are now solid, but the `run_id=None` / no-pod recovery branch is still not fully implementable as written.
- Quality: remaining gap. Two sections describe different recovery outcomes for the same crash boundary.
- Security: covered for this pass. No new major security issues found.

## Open Questions / Assumptions

- Assumed `orphaned` is not intended to be a terminal dead-end state for a recoverable pre-submit crash.
- Assumed the design goal is to recover from "persisted but not yet submitted" crashes without manual intervention.

## Summary

This is close. The prior blockers are fixed, and the remaining issue is now narrow: the plan should define one concrete recovery outcome for `dispatched` steps with `run_id=None` when no pod exists, instead of leaving that branch split between "re-spawn" and "mark orphaned."
