# Review: phase-8-tasks.md

## Findings

### 1. Medium (Quality/Functionality): the top-level durable recovery contract still contains stale `mark orphaned` behavior for `run_id=None`
The latest revision fixes the concrete Task 5 recovery path: when `run_id is None` and no pod is reachable, the poller now marks the step **failed** and routes it through `ingest_step_result()` + `advance_workflow()`, so normal retry policy decides what happens next. That resolves the prior functional gap.

But one older summary block still says something different. In the top-level "Durable Recovery Handle" section, the recovery poller steps still say:

- "If `run_id` is None: check if pod is reachable; if yes, re-submit; if not, mark orphaned"

That now conflicts with both the earlier "Persist-Before-Spawn" section and the concrete Task 5 flow, which both say the no-pod case becomes a failed terminal attempt that then flows through normal retry policy.

Why it matters:
- this is now a stale competing source of truth inside the same plan
- implementers could follow the summary section and reintroduce the dead-end `orphaned` behavior the later task text just fixed
- the skill's LGTM bar explicitly calls out stale-text and dependency/verification consistency issues

Recommended fix:
Update the "Durable Recovery Handle" numbered list so the `run_id=None` / no-pod branch matches Task 5 exactly: mark the step failed with the dispatch-interrupted error, call `ingest_step_result()`, then `advance_workflow()` so retry policy handles the next action.

## Perspective Check
- Functionality: covered. The concrete recovery semantics now look implementable.
- Quality: remaining gap. One stale summary block still contradicts the main task flow.
- Security: covered. No new major security issues found.

## Open Questions / Assumptions

- Assumed the Task 5 flow is the intended final source of truth for the `run_id=None` recovery branch.

## Summary

This looks very close to ready. The remaining issue is no longer a missing mechanism; it is a stale contradictory summary line that should be brought into alignment before `LGTM`.
