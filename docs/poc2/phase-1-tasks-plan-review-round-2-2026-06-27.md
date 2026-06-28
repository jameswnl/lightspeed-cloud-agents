# Review: `phase-1-tasks.md`

## Findings

### 1. Major (Quality): the new contracts are not propagated into the task list, so the doc still has competing implementation instructions

The new top-level contract section fixes several of the original design gaps, but the later task breakdown still describes the old behavior in multiple places. The biggest examples:
- Task 7 still shows `approve(step_name, decision)` even though the new approval contract requires a selected option id.
- Task 9 still says `approvedOption` is built from analysis output by role, which conflicts with the new `selected_option_id` lookup contract.
- Task 15 still lacks the new cleanup tests listed in the contract section.

Why it matters:
An implementer can still follow the stale task text and build the wrong behavior even though the correct behavior now exists earlier in the document.

Recommended fix:
- update Tasks 7, 9, and 15 so they match the new contracts directly
- remove or rewrite any stale bullets that still describe the superseded design

### 2. Major (Quality): the API version drift is only partially fixed; stale `v2` references remain in the task plan

The API contract now says Phase 1 uses `/v1/workflows/*`, and Task 12 uses `/v1/workflows/*`, but the surrounding task text still says:
- `### Task 12: v2 API endpoints`
- dependency graph entry `Task 12 (v2 API)`
- Task 13 says `Register v2 API router`

Why it matters:
This leaves the plan with two API versions in the same phase doc and makes it unclear what router name, compatibility story, and endpoint surface should actually ship.

Recommended fix:
- rename Task 12 to `Workflow API endpoints`
- update the dependency graph and Task 13 text to reference the same `/v1` contract everywhere

### 3. Major (Functionality): the crash-cleanup contract exists, but the implementation tasks still do not require the mechanism that makes it work

The new contract introduces label-based orphan cleanup and two new tests, but the concrete implementation tasks still stop at `finally: spawner.destroy(pod_name)` and do not require:
- labels on spawned workloads,
- a cleanup path that can find leaked workloads after worker death,
- or implementation of the new cleanup tests.

Why it matters:
The recovery story is still not actually tasked. The contract says what should exist, but the implementation plan does not yet make anyone build it.

Recommended fix:
- add explicit task bullets requiring workflow/step/attempt labels on spawned workloads
- add the label-based cleanup mechanism to the spawner/activity work
- add the new cleanup tests to Task 15 explicitly

### 4. Medium (Security): the credential contract is clearer, but the implementation tasks still describe only the Kubernetes path

The contract section now explains the Podman trust-boundary compromise, but Task 8 still says credentials are provided via `SecretKeyRef`, which is only the Kubernetes mechanism. The Podman-specific credential propagation path is not reflected in the activity or spawner tasks.

Why it matters:
This can still lead to an implementation that handles K8s correctly while leaving the Podman path under-specified, despite Phase 1 claiming support for both targets.

Recommended fix:
- update Task 8 and Task 10 so they describe both K8s and Podman credential injection paths
- make the Podman credential behavior part of the explicit acceptance/verification path

### 5. Medium (Quality): the E2E plan is still mostly happy-path and does not verify the hardest state transitions

The doc still does not add an end-to-end failure-path scenario for retry, escalation, or cleanup. The new contract section improved semantics, but the test plan still proves mostly success-path behavior.

Why it matters:
The most failure-prone parts of this design are retry classification and cleanup across boundaries. Those remain largely unverified at the E2E level.

Recommended fix:
- add at least one E2E for `502 -> retry -> eventual success` or `retry exhaustion -> escalated terminal state -> cleanup verified`

## Perspective Check
- Functionality: improved, but cleanup/recovery is still not concretely tasked end-to-end.
- Quality: still has stale superseded text in the task breakdown and dependency graph.
- Security: improved, but Podman credential handling is still not fully reflected in the implementation tasks.

## Open Questions / Assumptions

- I assumed the contract section is intended to replace the stale task details, not sit alongside them indefinitely.
- I assumed Phase 1 still aims to be implementation-ready rather than only directionally correct.

## Summary

This revision is materially better and resolves most of the original design gaps at the contract level. The remaining problem is consistency: the task list still contains stale instructions from the previous design, and a few key recovery and Podman details are not yet turned into concrete implementation work. 
