# Review: Phase 2 implementation plan

## Findings

### 1. Blocker: `T19`'s circuit breaker design cannot actually provide provider-level production protection
The task is explicitly "Circuit breaker for LLM provider," but the proposed `CircuitBreaker` is a single in-memory counter in one runner process with no provider key and no cross-replica/shared state. In a multi-replica deployment, one runner can open while the others keep spawning sandboxes; in a multi-provider deployment, repeated failures on one provider can trip the breaker for every provider handled by that process. That means the plan's implementation mechanism does not match the promised runtime behavior.

Recommended fix: define the breaker scope explicitly and make the mechanism match it. At minimum, key breaker state by provider/model (or provider endpoint) and decide whether Phase 2 requires shared state across replicas. If shared state is out of scope, narrow the claim to per-process protection and say so clearly in the plan and verification section.

### 2. Major: `T17` no longer covers the full alerting scope promised by the parent roadmap
The parent implementation plan says `T17` should alert on step failure rate, orphaned pods, Temporal worker heartbeat, and LLM provider errors. This phase plan only specifies alerts for step failure rate, sandbox cleanup failures, and runner `up == 0`. That is a real scope regression:
- cleanup failures are not the same thing as orphaned pods
- `up{job="workflow-runner"} == 0` is not a Temporal worker heartbeat signal
- there is no alert for LLM provider errors at all

Recommended fix: either restore the missing alert types or explicitly narrow the parent task and update the source plan accordingly. If Temporal worker health is the intent, define the actual signal (for example `readyz`, a Temporal connectivity metric, or an explicit heartbeat metric) rather than substituting a basic process-up check.

### 3. Major: `T7`'s verification plan still misses the definitions API, even though the task scope includes it
The Phase 2 plan correctly points to the LGTM'd `T7` design, but the verification checklist only mentions unauthorized/authorized workflow triggers, approval identity, and policy rule enforcement generally. The problem statement for `T7` explicitly includes submit/modify workflow definitions, and the design now models definition actions separately (`VIEW_DEFS`, `MANAGE_DEFS`). As written, this phase plan could be marked complete while `GET /definitions`, `GET /definitions/{name}`, or `POST /definitions` remain unprotected or only partially covered.

Recommended fix: add definition-surface verification items and tests to the phase plan, not just to the linked design doc. At minimum, include checks that unauthorized definition read/manage operations return `403` and authorized ones succeed.

## Perspective Check
- Functionality: major gaps remain in `T19` breaker semantics and `T17` alert coverage; both currently under-deliver relative to the behaviors the plan claims.
- Quality: the plan is mostly well-structured, but `T17` regresses scope from the parent roadmap and `T7`'s checklist does not fully cover the documented endpoint surface.
- Security: `T7` still needs definition-endpoint verification in the phase plan so the access-control scope cannot silently regress during implementation.

## Open Questions / Assumptions
- Is Phase 2 expected to harden multi-replica production behavior now, or is per-runner/local protection acceptable for `T19` as an interim step?
- For `T17`, is the intention to alert on runner liveness, Temporal connectivity, or an actual worker heartbeat signal?

## Summary
Not LGTM. `T21` and `T24` look straightforward, and `T7` is on a much stronger foundation thanks to the separate design review, but the current Phase 2 plan still has a real mechanism/scope mismatch in `T19`, drops required alerting coverage in `T17`, and under-specifies `T7` verification for the definitions API.
