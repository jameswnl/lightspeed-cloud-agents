# Review: docs/rbac.md (Round 2)

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-07-01

## Verdict: Approve with 2 gaps

The doc is accurate and well-structured. Two gaps between the approved design and the current implementation/docs.

## Gaps

### 1. `risk_levels` condition missing from implementation and docs

The LGTM'd design (`t7-rbac-design.md`) includes `risk_levels` as a policy condition for approve actions:

```yaml
- identity: "team:platform"
  actions: [approve]
  workflows: ["*"]
  conditions:
    risk_levels: [high, critical]  # can only approve high-risk steps
```

This enables per-risk-level approval control — a user can approve low-risk steps but not high-risk ones. The operator achieves equivalent behavior through its per-step approval in the ProposalApproval CRD.

**Status**: Not implemented in `PolicyFileAuthorizer` (only `require_owner` is implemented). Not documented in `rbac.md`. The design was approved with this feature.

**Decision needed**: Implement now (adds ~20 lines to `policy_authorizer.py` + tests), or explicitly defer with a tracking item. Either way, the design, code, and docs should agree.

### 2. No E2E tests for RBAC

Integration tests exist (`tests/integration/test_rbac_integration.py` — 199 lines, uses `TestClient` + `PolicyFileAuthorizer` with real middleware flow). But there are no E2E tests that exercise RBAC against a real Temporal server.

The implementation plan's test section specifies:
```
tests/integration/:
  test_policy_file_loaded_and_enforced
```

This exists. But the test plan (`temporal-test-plan.md`) also specifies E2E scenarios:
```
test_unauthorized_trigger_returns_403
test_authorized_trigger_succeeds
test_unauthorized_approve_returns_403
test_approver_identity_recorded_in_state
```

These E2E tests are NOT in `tests/e2e/temporal/test_temporal_e2e.py`. The integration tests cover the API + policy layer via `TestClient`, but don't verify the full flow: API → Temporal signal → workflow state with approver identity persisted → queryable via status endpoint.

**Risk**: The ApproverInfo persistence fix (blocker #1 from the implementation review) was verified by unit tests but not by an end-to-end test. An integration test that starts a workflow, sends an approve signal with identity, and queries the status to verify `approver_username` appears would catch wiring issues between the API layer, Temporal signal, and workflow state.

**Decision needed**: Add at least one E2E test that verifies the full approve flow with identity persistence, or document why integration-level coverage is sufficient.
