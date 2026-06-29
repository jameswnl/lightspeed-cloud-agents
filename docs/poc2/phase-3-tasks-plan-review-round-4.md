# Review: `phase-3-tasks.md`

## Findings

### 1. Medium (Quality): the verification section still mixes two different workflow-runner health surfaces

The task list defines new workflow-runner probes as `/livez` and `/readyz`, and the probe semantics section below uses `/livez` and `/readyz` consistently. But the Kind-based smoke checklist still says the workflow-runner's `/healthz`, `/readyz`, and `/metrics` should respond correctly.

That leaves a stale second source of truth for the health surface: one reader will think `/healthz` is still part of the contract, another will implement only `/livez` and `/readyz`.

Recommended fix:
- update the Kind-based smoke section to use the same endpoint set as the task list
- if `/healthz` is intentionally retained as an existing compatibility endpoint, say so explicitly and state whether `/livez` replaces it or supplements it

## Perspective Check

- Functionality: no new major issues found
- Quality: one remaining stale endpoint reference in the verification section
- Security: no new major issues found beyond the explicitly deferred single-team boundary

## Open Questions / Assumptions

- I assumed `/livez` and `/readyz` are intended to be the canonical new workflow-runner probe endpoints for this phase.
- I assumed the `/healthz` mention in the smoke checklist is stale rather than an intentional compatibility requirement.

## Summary

The plan is effectively there, but not yet fully self-consistent. One stale `/healthz` reference remains in the verification section.
