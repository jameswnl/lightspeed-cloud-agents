# Review: T7 RBAC design

## Findings

### 1. Medium: the configuration surface still advertises `k8s-sar` as a supported authz mode even though the backend is explicitly deferred
The design now clearly defers `K8sSARAuthorizer`, which is good, but the configuration section still says `WORKFLOW_AUTHZ=none|policy|k8s-sar` and the Helm values example still lists `none | policy | k8s-sar`. That leaves the doc with two competing sources of truth: the backend section says SAR is not in scope for this phase, while the configuration section still presents it as an operator-selectable mode. That is likely to turn into either a broken deployment option or an unplanned implementation obligation.

Recommended fix: either remove `k8s-sar` from the Phase 2 configuration surface entirely, or mark it as reserved/not yet implemented in the config table and Helm example. The implementation plan should match that same scoping.

## Perspective Check
- Functionality: the core T7 runtime contracts now look implementable.
- Quality: the remaining issue is mostly a scope/config consistency problem rather than a missing runtime mechanism.
- Security: no new major trust-boundary gaps stood out after this revision.

## Open Questions / Assumptions
- If `k8s-sar` is intentionally kept in config for forward compatibility, should the server reject it explicitly at startup until the backend exists?

## Summary
Still not LGTM, but very close. The design is now substantially more implementation-ready; the remaining work is to make the configuration surface consistent with the decision to defer the SAR backend.
