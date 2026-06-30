# Review: `phase-1-tasks.md`

## Findings

### 1. Major (Quality): the label contract is still inconsistent between the cleanup section and the spawner task

The crash-boundary cleanup contract defines labels as:
- `cloud-agents/workflow-id`
- `cloud-agents/step-name`
- `cloud-agents/attempt`

But the Task 10 spawner bullets now say:
- `cloud-agents/workflow-id`
- `step-name`
- `attempt`

Why it matters:
This is a real implementation contract, not a wording nit. The cleanup story depends on being able to reconstruct and select orphaned workloads by a stable label set. If the labels are not named consistently across the doc, different implementations or tests can target different selectors and the crash-recovery path becomes ambiguous again.

Recommended fix:
- choose one exact label set and use it everywhere in the phase doc
- if the intended scheme is the namespaced version from the cleanup contract, update Task 10 to match it exactly

## Perspective Check
- Functionality: still effectively covered, but cleanup selectors need one exact contract.
- Quality: one remaining source-of-truth mismatch in the label names.
- Security: no new issues found in this pass.

## Open Questions / Assumptions

- I assumed the labels are meant to be part of a stable external contract for cleanup and tests, not an implementation detail that can vary by spawner.

## Summary

This is very close. The remaining issue is a single label-name inconsistency in the cleanup contract versus the spawner task.
