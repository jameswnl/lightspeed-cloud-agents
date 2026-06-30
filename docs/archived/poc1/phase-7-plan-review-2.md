# Review: phase-7-tasks.md

## Findings

### 1. High: Task 5 still does not define an automatic cleanup path for orphaned running Jobs
The revised Task 5 correctly drops `ownerReferences`, but the replacement cleanup story is still incomplete. `ttlSecondsAfterFinished` only cleans up Jobs that already reached a terminal state. The recovery poller is described as marking timed-out dispatched steps failed in workflow state, but the task never says who deletes or stops the corresponding still-running Kubernetes Job. The final bullet is only a manual `kubectl delete` command, which is an operational workaround rather than an implementation mechanism.

Why it matters:
- a runner crash or lost callback can still leave a live orphaned Job consuming cluster resources
- the stated problem is "orphaned Jobs persist", but the task only automates cleanup for completed Jobs
- implementers still do not have a concrete contract for whether the poller, spawner, or some other component is responsible for deleting stale Jobs

Recommended fix:
- make the recovery path explicit: when a dispatched step times out or is declared orphaned, the recovery poller should call a spawner cleanup path that deletes the backing K8s Job
- keep TTL as cleanup for completed Jobs, not as the sole cleanup mechanism
- add verification that a runner crash / lost callback leaves no long-running orphaned Job after recovery executes

### 2. Medium: the dependency and verification sections still describe the old owner-reference design
The core task text was updated, but the later sections still refer to `Task 5 (owner refs)` and still claim `Delete workflow runner → spawned Jobs cleaned up (owner refs)`. That no longer matches the revised design and makes the doc internally inconsistent.

Why it matters:
- readers cannot tell which cleanup model is the actual source of truth
- implementation sequencing and test expectations still point to the superseded design
- this kind of drift is exactly how task docs turn into contradictory implementation guidance

Recommended fix:
- rename the dependency node to match the new Task 5 title/content
- replace the robustness verification bullet with checks for TTL cleanup of completed Jobs plus recovery-driven deletion of orphaned running Jobs

## Perspective Check
- Functionality: remaining gaps. The revised cleanup design is better aligned with the stateless model, but it still lacks an automatic mechanism for deleting orphaned running Jobs.
- Quality: remaining gaps. The task body and the dependency/verification sections are not yet fully synchronized.
- Security: no major new issues found in the updated revision. The prior auth and fail-closed `risk_level` concerns were addressed at the plan level.

## Open Questions / Assumptions
- Should the recovery poller directly delete stale Jobs, or should it delegate deletion through `KubernetesSpawner.destroy()`?
- Is manual orphan cleanup intended only as an emergency runbook step, or is it currently standing in for missing product behavior?

## Summary
Much closer. The major auth, owner-reference, and fail-open risk issues from the first pass were fixed. The remaining problem is narrower but real: Task 5 still does not fully specify who automatically cleans up orphaned running Jobs, and the trailing dependency/verification sections still reflect the old design.
