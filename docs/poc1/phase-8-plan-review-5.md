# Review: phase-8-tasks.md

## Findings

### 1. Medium (Quality): one stale summary sentence still says the `run_id=None` branch can end in `mark orphaned`
The main recovery behavior is now correct and consistent in the concrete sections:

- `Persist-Before-Spawn` says the no-pod / `run_id=None` case becomes a failed attempt that flows through retry policy
- Task 5 says the same thing explicitly via `ingest_step_result()` + `advance_workflow()`

But the top-level summary sentence under "Durable Recovery Handle" still says:

- "`run_id` is initially `None` when persisted before spawn. After successful async submission, `dispatch_async()` updates the step output with the `run_id` and re-persists. The recovery poller handles both cases: with `run_id` (poll for result) and without (**re-submit or mark orphaned**)."

That leaves one stale competing phrase after the rest of the doc moved to the failed-attempt + retry-policy model.

Why it matters:
- this is still a conflicting source of truth in the final draft
- the document is otherwise very close, so this kind of stale sentence is now the main thing keeping it from a clean `LGTM`

Recommended fix:
Change that summary sentence so the `run_id=None` branch matches the concrete flow:
- with `run_id`: poll for result
- without `run_id`: re-submit if the pod is reachable, otherwise mark the attempt failed and route through normal retry policy

## Perspective Check
- Functionality: covered. The concrete runtime semantics look implementable.
- Quality: remaining gap. One stale summary sentence still contradicts the main task flow.
- Security: covered. No new major security issues found.

## Open Questions / Assumptions

- Assumed the concrete Task 5 flow is the intended final source of truth and this remaining mismatch is accidental stale text.

## Summary

This is effectively one stale sentence away from `LGTM`.
