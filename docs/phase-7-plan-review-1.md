# Review: phase-7-tasks.md

## Findings

### 1. High: the plan weakens the production auth contract back to a shared secret
Phase 7 says the same `AGENT_API_TOKEN` is shared between the runner and all spawned pods, and treats service-account-based identity as a future enhancement for Kubernetes deployments. That conflicts with the earlier production design, which reserved shared-secret auth for dev and called for Kubernetes-native identity in production.

Why it matters:
- a compromised spawned pod can still impersonate the runner or peer agents
- the original lateral-call problem is only narrowed, not actually closed
- the Phase 7 task doc silently changes an earlier trust-boundary contract instead of calling it out

Recommended fix:
- keep shared-secret auth only for Podman/dev
- require Kubernetes production deployments to use projected service-account tokens or another per-pod/per-run scoped credential
- if the design is intentionally changing, update the architecture and production-design docs to describe the new trust model explicitly

### 2. High: owner-referencing step Jobs to the runner pod conflicts with the stateless failover model
Task 5 says spawned Jobs should point their `ownerReferences` at the workflow runner `Deployment/Pod` so that garbage collection cleans them up if the runner disappears. That does not fit the Phase 6 stateless multi-replica design. If Jobs are owned by the runner Pod, normal restart or rollout can garbage-collect in-flight work that another replica was supposed to recover. If they are owned by the Deployment instead, runner crashes do not solve the orphan problem the task claims to address.

Why it matters:
- owner refs to the runner Pod can delete live step Jobs during normal failover
- owner refs to the Deployment do not provide the claimed crash cleanup semantics
- the plan only mentions `HOSTNAME`, which is not enough to define a correct owner reference contract

Recommended fix:
- do not tie step Jobs to an ephemeral runner Pod
- keep cleanup explicit via the recovery poller and TTL, or introduce a durable workflow-run parent object if GC-style ownership is required
- tighten the task text so it describes one implementable ownership model, not `Deployment/Pod`

### 3. High: `risk_level` still fails open because keyword inference remains on the production path
Task 2 adds explicit `risk_level`, but it also keeps the current keyword-based classifier whenever the field is omitted. That means the exact bug this task is meant to eliminate still exists as a runtime behavior, just with a warning.

Why it matters:
- missing `risk_level` still allows substring-based misclassification
- approval gating remains vulnerable to naming accidents
- warnings are not a sufficient safety mechanism for a production approval boundary

Recommended fix:
- make `risk_level` mandatory for approval-relevant or executable steps, or
- fail closed when it is absent by routing the step to manual approval / highest-risk handling
- if inference is kept at all, make it debug-only and never a production approval signal

### 4. Medium: the phase intro claims all critical/high security issues are addressed, but one is explicitly deferred
The opening context says Phase 7 addresses all `3 critical + 2 high security issues` and that these issues must be fixed before production deployment. Later, Task 3b explicitly defers tool-origin validation as out of scope. That makes the phase framing internally inconsistent.

Why it matters:
- the document overstates what this phase actually fixes
- implementers and reviewers cannot tell whether production readiness depends on Task 3b
- the backlog/defer decision is reasonable only if the phase summary reflects it accurately

Recommended fix:
- either include a minimal mitigation for tool-origin validation in Phase 7, or
- revise the intro to say one high-severity item is intentionally deferred and explain why that is acceptable for the intended production scope

## Perspective Check
- Functionality: remaining gaps. The owner-reference cleanup design conflicts with the stateless multi-replica execution model established in Phase 6.
- Quality: remaining gaps. The task list and phase framing still contradict earlier production contracts and one another.
- Security: remaining gaps. Shared bearer auth and fail-open `risk_level` inference leave core production trust boundaries under-specified.

## Open Questions / Assumptions
- Is Phase 7 intentionally revising the Kubernetes production auth model, or was the shared-token approach meant only for Podman/dev?
- Is there a durable per-workflow Kubernetes object available that step Jobs could safely reference instead of the runner Pod?
- Should `risk_level` be mandatory for all non-read-only agent steps?

## Summary
Not LGTM yet. The direction is good, but the plan still has two major contract problems: Task 3 weakens the earlier production auth model, and Task 5 proposes ownership semantics that do not fit the stateless failover architecture. Tightening those two areas, and making `risk_level` fail closed, would make the phase much more implementation-ready.
