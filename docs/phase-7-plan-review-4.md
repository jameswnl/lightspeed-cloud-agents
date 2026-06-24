# Phase 7 Plan Review — Operator-Informed Perspective

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-06-24
**Context**: Reviewed after hands-on testing of the agentic operator on Kind (deployed in-cluster with GPT-5.5 SandboxAgent, hit real bugs, submitted 2 PRs). This review brings operator implementation experience to the Cloud Agents plan.

## Overall Assessment

The plan is well-structured and correctly prioritizes security fixes before robustness. The task decomposition is clean, dependencies make sense, and the scope is right — neither too ambitious nor too timid.

**Verdict: Approve with 4 specific concerns below.**

## Good Decisions

1. **Task 3b deferred (tool origin validation)** — Correct. Agent YAML is platform-team authored. Restricting `importlib` would break the framework's extensibility model. An optional allowlist in the backlog is the right compromise.

2. **Task 5 rejects `ownerReferences`** — Smart. In the operator, owner refs work because the controller Pod is the single long-lived owner. In a stateless multi-replica model, owner refs to the runner Pod would GC in-flight Jobs during normal rollouts. The TTL + recovery poller approach is architecturally consistent with the stateless design.

3. **Task 3 splits auth modes per deployment target** — Shared secret for Podman (single trust domain), projected SA tokens for K8s (per-pod identity). This matches each target's security model without forcing one pattern on both.

4. **Task 2 fails closed** — Unknown risk → high → manual approval. This is the safe default. The operator's ApprovalPolicy defaults to Manual for unconfigured steps, which is the same principle.

## Concerns

### 1. Task 4: Content-hash collision risk

The plan says "hash the step config (agent name, prompt hash, attempt number)". This is incomplete — **two different workflows** using the same agent with the same prompt at the same attempt number would produce the same hash, colliding on the same Job name.

**Fix**: The hash must include `workflow_id` + `step_name` + `attempt_number`. The operator avoids this by including `proposal.Name` in the result CR name (`resultCRName(proposal.Name, "analysis", index)`), making names globally unique per proposal.

### 2. Task 5: Recovery poller needs reconstructible Job names

The cleanup chain assumes the recovery poller can call `spawner.destroy(spawned_name)`. But if the runner crashes **after** spawning the Job but **before** persisting `spawned_name` to workflow state, the poller won't know what to clean up.

Content-hash naming from Task 4 actually solves this — the poller can **reconstruct** the expected Job name from the step config (which is persisted in the workflow definition) without having seen the original spawn. This is exactly how the operator's `EnsureAgentTemplate()` works: same inputs → same template name → idempotent retry and discoverable cleanup.

**Recommendation**: Explicitly document this property in Task 4 as a design requirement, not just a nice-to-have. Task 5's recovery poller depends on it.

### 3. Task 7: Permissions in context should be typed

The plan says "pass step permissions in context" via `request.context`. Currently `context` is an untyped `dict[str, Any]`. Passing permissions as an unstructured dict means the runner can't validate the shape at load time — it would fail at tool-filtering time with a confusing `KeyError`.

**Fix**: Define a `StepPermissions` dataclass (or Pydantic model) that the executor constructs and the runner validates. The advisory mode already has this pattern — `AdvisoryEnforcer` is a typed class, not a dict.

### 4. Task 10: Integration tests should cover retry → escalation

The plan scopes integration tests as "dispatch → execute → cleanup lifecycle". This misses the most complex state transitions: retry with failure context enrichment → max retries exhausted → escalation handoff. That's where the operator had its stale-result-CR bug (our PR #41) — the retry path exercised code that the happy path never touched.

**Recommendation**: Add a test case that fails a step twice (max_retries=2), verifies failure context is passed to the second attempt, then triggers escalation. Verify the escalation output includes both failure records.

## Minor Notes

- Task 1 mentions "K8s manifests" in the files list but the framework doesn't ship K8s manifests — the spawner creates Jobs programmatically. Clarify this means updating example deployment docs, not YAML files.
- Task 6 (`derive_status`) should also re-derive on `resume()`, not just on `load()`. A paused workflow that was manually patched in the DB could have inconsistent status.
- The dependency graph shows Tasks 6-8 as independent of Tasks 1-3, which is correct. But Task 7 (PermissionScope) has a soft dependency on Task 3 (auth) — if permissions include `service_account`, the spawner needs to support per-step SA, which overlaps with the SA token auth model.
