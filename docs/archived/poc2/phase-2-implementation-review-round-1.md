# Review: Phase 2 implementation round 1 (working tree before first commit)

## Findings

### 1. Major: `approval_policy` is not reachable from real workflow entrypoints
The workflow now consumes `input.approval_policy`, but the actual startup surfaces do not pass one in. `RunWorkflowRequest` has no `approval_policy` field, `build_temporal_router()` never forwards one into `WorkflowInput`, and stored `WorkflowDefinition` objects do not carry a policy either. Right now the "custom policy" behavior only exists in unit tests that mutate `WorkflowInput` directly, so API-started workflows still cannot exercise the claimed contract.

Recommended fix: add a typed approval-policy input on the real workflow entrypoint(s), propagate it into `WorkflowInput`, and add caller-side tests that prove a custom policy survives the API/definition path into `_handle_approval()`.

### 2. Medium: Tests do not prove the new event contract or caller-side policy contract
The new unit tests prove the happy-path timeout/auto-approval behavior inside the workflow, but they do not assert that `step.auto_approved` is emitted even though the implementation now treats that event as part of the contract. They also do not cover the external caller path for custom policy, which is why the first issue slipped through while the suite still passed.

Recommended fix: add a query/status assertion that verifies `step.auto_approved` appears in workflow events, and add API or definition-store tests that confirm custom policy configuration actually reaches the workflow runtime.

## Perspective Check
- Functionality: covered; found one major contract gap in policy propagation to real workflow starts.
- Quality: covered; tests pass, but they currently validate an internal-only path and miss the emitted-event contract.
- Security: covered; no major new trust-boundary regression found in this snapshot, and missing `risk_level` still fails closed to manual approval.

## Verification
- `git status --short`
- `git log -3 --stat --decorate`
- `git diff --name-only`
- Read: `src/agents/workflow/auto_approve.py`, `src/agents/workflow/temporal_models.py`, `src/agents/workflow/temporal_workflow.py`, `src/agents/workflow/temporal_api.py`, `src/agents/workflow/definition.py`, `tests/unit/agents/workflow/temporal/test_workflow.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/unit/agents/workflow/test_auto_approve.py`
- `uv run pytest tests/unit/agents/workflow/temporal/test_workflow.py -q` -> 10 passed

## Summary
The current Phase 2 implementation snapshot has the core auto-approval logic in place and the focused workflow tests are green, but the custom-policy contract is not wired through any real workflow entrypoint yet. I am keeping polling active and will re-review the whole requested Phase 2 scope once a follow-up commit looks settled.
