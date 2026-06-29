# Review: `phase-1-tasks.md`

## Findings

### 1. Major (Quality): approval and context-building tasks still describe the superseded design

The contract section now correctly defines approval selection by stable option id, but the implementation tasks still describe the older contract:
- Task 7 still says `approve(step_name, decision)` rather than including the selected option id required by the new approval contract.
- Task 9 still says `approvedOption` is built from analysis step output by role, which conflicts with the contract section that resolves it from `selected_option_id`.

Why it matters:
The doc still contains two competing implementation instructions for the core approval -> execute handoff, so an implementer could follow the stale task text and build the wrong behavior.

Recommended fix:
- update Task 7 so the signal contract matches the selected-option-id design
- update Task 9 so `approvedOption` is explicitly derived from the approval output plus the matching analysis option

### 2. Major (Quality): stale `v2` API references still remain in headings and dependency text

The endpoint bullets now use `/v1/workflows/*`, but the surrounding phase text still says:
- `### Task 12: v2 API endpoints`
- dependency graph entry `Task 12 (v2 API)`
- Task 13 bullet `Register v2 API router`

Why it matters:
The document still presents two API version stories in the same phase, which weakens the source-of-truth benefit of the earlier contract cleanup.

Recommended fix:
- rename Task 12 to something version-neutral or `/v1`-consistent
- update the dependency graph and Task 13 text to match the `/v1` contract everywhere

### 3. Major (Functionality): the crash-cleanup contract is still not turned into a concrete implementation task

The contract section says spawned workloads carry workflow labels and that label-based cleanup handles worker crashes, and the tests now cover that expectation. But no implementation task currently requires adding those labels or building the cleanup path itself. Task 8 still stops at `finally: spawner.destroy(pod_name)`, and Task 10 still does not mention workflow/step/attempt labels or orphan cleanup.

Why it matters:
The doc now tests for a recovery mechanism that is not yet actually assigned to implementation work. That leaves a gap between the promised behavior and the task breakdown.

Recommended fix:
- add explicit Task 8 and/or Task 10 bullets for workflow/step/attempt labels on spawned workloads
- add the label-based orphan cleanup mechanism to the implementation plan, not just the contract and tests

### 4. Medium (Security): Podman credential handling is still clearer in the contract than in the implementation tasks

The contract section now explains the Podman credential compromise, but Task 8 still phrases credentials as `SecretKeyRef`, and Task 10 still does not explicitly call out Podman credential propagation.

Why it matters:
The dual-target promise is better specified than before, but the implementation plan still leans Kubernetes-first in the actual work items.

Recommended fix:
- update Task 8 and Task 10 so they explicitly cover both K8s and Podman credential injection paths

## Perspective Check
- Functionality: mostly covered, but cleanup after crash is still not fully tasked.
- Quality: still not clean because stale approval and API-version instructions remain in the task list.
- Security: improved, but Podman credential handling is still more contractual than operational.

## Open Questions / Assumptions

- I assumed the desired end state is one internally consistent phase doc, not a contract section that overrides stale task bullets by implication.

## Summary

This pass is closer. The remaining issues are mostly consistency and taskability: the document's contracts are now stronger than the task breakdown underneath them. Once those stale task bullets are aligned with the newer contracts, the plan should be close to review-complete.
