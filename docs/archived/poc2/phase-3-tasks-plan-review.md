# Review: `phase-3-tasks.md`

## Findings

### 1. Blocker (Functionality): the plan says the sandbox path is still stubbed, but never schedules the work that would make it real

The context says Phases 1-2 left the entire sandbox execution path in stub mode and that this phase makes the system production-ready. But the task list only adds packaging, observability, docs, network policy, CI, and two upstream sandbox PRs. There is no task to replace the stub with a real `spawn -> wait -> authenticated POST /v1/agent/run -> destroy` path, no task to define the worker-to-sandbox auth contract, and no task to prove live cleanup/retry behavior on that real path.

That leaves the phase's main promised behavior without an implementation task.

Recommended fix:
- add a P0 task that explicitly wires the real sandbox execution path
- define its runtime contract: spawn/destroy lifecycle, auth token delivery, cleanup/idempotency, and error classification
- make later productization tasks depend on that task
- require at least one non-stub E2E that proves the real path runs

### 2. Major (Quality): the upstream sandbox fixes are modeled as independent even though later tasks depend on them

T12 and T13 reintroduce the upstream sandbox changes for `executionResult` formatting and HTTP 502 infrastructure errors, but the dependency graph marks them independent. At the same time, T8 documents the sandbox contract and T18 tests it. That creates silent coupling: the contract is not actually stable unless the upstream behavior is available through a merged PR, a vendored patch, or a documented local fallback.

As written, the phase can finish its docs and tests while still having no final source of truth for the behavior those docs and tests claim.

Recommended fix:
- make T8 and T18 depend on T12 and T13, or
- define the fallback plan if upstream review is still pending
- state what counts as completion when the upstream PRs are open but not merged

### 3. Major (Security): the phase claims productization while the architecture still says production authorization is deferred

The companion architecture doc still says Phases 2-4 enforce authentication but not authorization, and that real RBAC for who can trigger, approve, and view workflows is deferred to later work. `phase-3-tasks.md` still frames this phase as production-ready, but only hardens Temporal TLS and egress networking. There is no task for API authorization, approval-role scoping, or durable approval audit logging.

That is a trust-boundary gap, not just a backlog omission. A workflow system with approval gates is not meaningfully productized if any authenticated caller can approve them.

Recommended fix:
- either narrow the claim to operational hardening for single-team deployments, or
- add explicit tasks for trigger/approve/view authorization, approval RBAC, and approval audit trail

### 4. Major (Quality): the verification plan does not test the production-only contracts introduced by this phase

T10 adds CI E2E with Temporal and PostgreSQL service containers, and T18 adds a sandbox HTTP contract test. That does not validate the riskiest new production-facing work in this phase: Helm deployment shape, K8s network policies, Temporal TLS secret wiring, readiness/liveness behavior, metrics exposure, or the live spawned-sandbox path on a real cluster.

So the plan promises production hardening, but its verification path mostly proves a local/containerized happy path.

Recommended fix:
- add an environment-specific verification section
- keep fast service-container CI, but add at least one Kind-based smoke/E2E path
- explicitly verify worker deployment, probes, metrics, TLS config, and network-policy behavior in a real K8s-shaped environment

### 5. Medium (Quality): the phase framing is stale across companion docs, weakening the rollout story

`phase-2-tasks.md` says Phase 2 is the final phase, while `phase-3-tasks.md` introduces a new productization phase, and the companion architecture still defers access control and escalation handoff to later work. That makes it unclear which guarantees Phase 2 actually achieved and which ones are still intentionally deferred.

This is mostly a documentation consistency problem, but here it directly affects scope, sequencing, and reviewability.

Recommended fix:
- add a short supersession note to `phase-3-tasks.md`
- state which Phase 2 assumptions are being revised
- call out which deferred architecture items remain intentionally out of scope for Phase 3

## Perspective Check

- Functionality: remaining gaps around the core non-stub sandbox execution path and the upstream behaviors it depends on
- Quality: remaining gaps in dependency modeling, testability of production claims, and stale phase sequencing
- Security: remaining gaps in authorization, approval scoping, and the definition of what "production-ready" means for this trust boundary

## Open Questions / Assumptions

- I assumed "production-ready" means more than single-team or internal-only deployment.
- I assumed the sandbox path is genuinely still stubbed at the start of this phase, not already being replaced in another unpublished companion doc.
- I assumed there is no hidden local fork already carrying the upstream sandbox changes.

## Summary

The backlog is reasonable as an ops-hardening phase, but it is not yet self-consistent as a true productization plan. The biggest gap is that it hardens packaging and observability around a sandbox path the doc itself says is still stubbed, while also leaving key authorization and upstream contract dependencies effectively out of band.
