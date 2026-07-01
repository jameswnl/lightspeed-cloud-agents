# Review: docs/rbac.md

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-07-01

## Verdict: LGTM

The doc is accurate, well-structured, and consistent with the implementation.

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

## Notes (not issues)

- The design doc (`t7-rbac-design.md`) shows `risk_levels` as a policy condition for approve actions, but it's not implemented in `PolicyFileAuthorizer` and not documented in `rbac.md`. This is consistent — doc matches code, not the aspirational design. Can be added later.

- The comparison with the operator's approach (K8s RBAC, mutating webhook, dynamic Roles) is not in this doc, which is appropriate — this is a user-facing reference doc, not an architecture comparison. That context lives in `poc2/operator-comparison-code-review.md`.
