# Review: T7 RBAC design

## Findings

### 1. Major: the design does not define the required fail-closed relationship between authentication and authorization
The proposal now defines `CallerIdentity` and `get_caller_identity()`, but it still allows a path where authorization is enabled while authentication is effectively absent. In the example dependency, a missing `request.state.caller_identity` falls back to `CallerIdentity(username="anonymous", auth_mode="shared_secret")`. That may be reasonable for explicit shared-secret deployments, but if `AUTH_REQUIRED=false` or identity extraction fails unexpectedly, the server can silently convert an unauthenticated request into an `"anonymous"` principal and continue into authorization. That weakens the security boundary precisely where T7 is supposed to tighten it.

Recommended fix: define a fail-closed contract explicitly. For example:
- `WORKFLOW_AUTHZ=policy` requires authentication to be enabled
- `get_caller_identity()` should raise when authz is enabled but no validated identity is present
- the `"anonymous"` identity should only be produced by an explicit shared-secret auth mode, not as a generic fallback for missing request state

### 2. Major: the definition API surface is still under-specified in the authorization model
The design improves workflow-run authorization, but it still does not cleanly model the definition endpoints that already exist in the real API: `POST /definitions`, `GET /definitions`, and `GET /definitions/{name}`. `WorkflowAction` only has one definitions-related action, `MANAGE_DEFS`, which could plausibly cover create/update, but there is no explicit read/list action for definitions. That leaves the design with an incomplete resource/action contract for one of the endpoints called out in the problem statement.

Recommended fix: define definition resources and actions explicitly. For example, add separate actions for `VIEW_DEFS` and `MANAGE_DEFS`, and specify how policy rules distinguish definition read access from workflow-run view access. The API wiring section should show authorization checks for all three definition endpoints, not just `/run` and `/approve`.

### 3. Medium: namespace derivation in SA-token mode is described as if TokenReview returns it directly
The draft now defines namespace persistence, which is good, but the text still says namespace comes from “the ServiceAccount's namespace in the TokenReview response.” In practice, TokenReview gives you `username`, `uid`, and `groups`; for ServiceAccounts the namespace is encoded in the username format (`system:serviceaccount:<namespace>:<name>`), not returned as a dedicated namespace field. That detail matters because this namespace becomes part of the persisted authorization context and later policy decisions.

Recommended fix: tighten the contract and say namespace is **parsed from** the authenticated ServiceAccount username (or another explicitly documented field if you plan to use one), with validation/error behavior if the username is not in the expected ServiceAccount form.

## Perspective Check
- Functionality: the design is much closer now, but the definitions API still lacks a complete action/resource model.
- Quality: the main remaining gap is incomplete contract definition for authz-enabled deployments and for the definitions surface.
- Security: identity handling is substantially improved, but the design still needs an explicit fail-closed rule so missing request identity cannot degrade into permissive `"anonymous"` authorization by accident.

## Open Questions / Assumptions
- Should `GET /definitions` be broadly readable to authenticated callers, or scoped the same way as workflow-run visibility?
- If authz is enabled in shared-secret mode, is the intended deployment posture truly “single trusted team only,” or do you want the server to reject that combination unless a policy explicitly opts in?

## Summary
Still not LGTM, but much closer. The core runtime contracts for identity extraction and persisted workflow authz context are now in place. The remaining work is to make the auth/authz interaction fail closed and to fully define how the existing definitions endpoints fit into the RBAC model.
