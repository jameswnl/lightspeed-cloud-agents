# Review: docs/rbac.md

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-07-01

## Verdict: Approve with 1 gap

The doc is accurate, well-structured, and consistent with the implementation. One feature from the approved design is missing from both implementation and documentation.

## What's good

- Quick start gets someone running in 3 steps — env vars, policy YAML, mount
- Identity table matches the actual `CallerIdentity` extraction in `auth.py`
- Actions table correctly maps to actual endpoint routes (`/v1/workflows/*`)
- Fail-closed section is precise: authz + no identity → 401, no rule → 403, context lookup fails → 503
- Shared secret limitation is honestly stated ("deployment-level auth, not per-user RBAC")
- `require_owner` condition is documented and implemented (`policy_authorizer.py:208`)
- Approver identity output format matches the actual workflow state structure
- Both Helm and Podman mounting examples included

## Verified against code

| Doc claim | Code location | Accurate? |
|---|---|---|
| CallerIdentity fields (username, uid, groups) | `authorization.py:CallerIdentity` | Yes |
| 6 WorkflowAction values | `authorization.py:WorkflowAction` | Yes |
| WorkflowAuthzContext persisted in Temporal | `temporal_workflow.py:run()` stores `input.authz_context` | Yes |
| Approver username/uid in step output | `temporal_workflow.py:_handle_approval()` includes in result | Yes |
| Fail-closed when authz enabled + no identity | `temporal_api.py:get_caller_identity()` raises 401 | Yes |
| `require_owner` condition | `policy_authorizer.py:208-209` | Yes |
| Definition endpoints authorized | `temporal_api.py` — `VIEW_DEFS` on GET, `MANAGE_DEFS` on POST | Yes |
| Endpoint paths | Router prefix `/v1/workflows` + `/definitions`, `/definitions/{name}` | Yes |

## Gap

### `risk_levels` condition missing from implementation and docs

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

### No E2E tests for RBAC

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
test_k8s_sar_on_kind_cluster (deferred with SAR)
```

These E2E tests are NOT present in `tests/e2e/temporal/test_temporal_e2e.py`. The integration tests cover the API + policy layer via `TestClient`, but don't verify the full flow: API → Temporal signal → workflow state with approver identity persisted → queryable via status endpoint.

**Risk**: The ApproverInfo persistence fix (blocker #1 from the implementation review) was verified by unit tests but not by an end-to-end test. An integration test that starts a workflow, sends an approve signal with identity, and queries the status to verify approver_username appears would catch any wiring issues between the API layer, Temporal signal, and workflow state.

**Decision needed**: Add at least one E2E test that verifies the full approve flow with identity persistence, or document why integration-level coverage is sufficient.
