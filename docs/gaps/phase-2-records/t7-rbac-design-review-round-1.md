# Review: T7 RBAC design

## Findings

### 1. Blocker: the design says identity extraction is “already done,” but the current auth layer does not expose caller identity to the API or workflow paths
The proposal treats identity extraction as complete and says “no changes needed here,” but the current middleware only returns `401`/`200` based on token validity. It does not attach `username`, `uid`, `groups`, or even a stable caller object to the request context, and there is no existing `CallerIdentity` or `get_caller_identity` seam in the workflow API. That means all three backends in Layer 2 depend on data the runtime does not currently produce, and the approval-capture flow in Layer 3 cannot actually record a server-authoritative approver identity yet.

Recommended fix: make identity extraction an explicit first-class design step, not “already done.” Define the request-context contract precisely for each auth mode:
- shared secret: what stable identity, if any, is attached
- TokenReview: which fields from the review response become `username`, `uid`, and `groups`
- JWT: which claims map to user/team identity

### 2. Blocker: the design never defines the persisted authorization context needed for later `approve` / `view` / `cancel` checks
Authorizing `trigger` can use request-local inputs, but `approve`, `view`, and `cancel` happen later and only have `workflow_id` plus maybe `step_name`. The design does not define where the workflow’s owning team/user/namespace/scope is stored so the authorizer can make those later decisions. The current workflow status/query path only exposes steps and events, not immutable authz metadata. This is especially important for:
- policy rules scoped by team/user/workflow
- SAR checks that need a namespace/resource scope
- approval rules conditioned on step risk level

Without persisted authz context captured at trigger time, the server cannot reliably decide whether a later caller may act on an existing workflow.

Recommended fix: define an immutable authz metadata record captured when the workflow is started and stored with workflow state, for example owner identity, owner groups/teams, namespace/project scope, workflow definition name/version, and any approval-relevant attributes. Then define exactly how `approve`, `view`, and `cancel` look that metadata up before authorizing.

### 3. Major: the `K8sSARAuthorizer` resource model is under-specified and conflicts with the real API surface
The SAR backend invents one synthetic K8s resource, `workflows` in `cloud-agents.lightspeed.redhat.com`, and maps all operations onto it. But the actual HTTP surface has at least two distinct resource families:
- workflow runs (`/run`, `/{id}`, `/{id}/approve`, `/{id}/cancel`)
- workflow definitions (`/definitions`, `/definitions/{name}`)

The design also relies on `resource.namespace`, but workflow runs are Temporal executions, not namespaced K8s objects, so the namespace authority is undefined. As written, this gives the system two competing sources of truth: HTTP resources in FastAPI and synthetic SAR resources in RBAC, without a precise mapping between them. That makes the Helm RBAC examples and the SAR test plan too weak to prove the intended behavior.

Recommended fix: define the authorization resource model explicitly before implementation. At minimum, split run-time and definition-time resources (for example `workflowruns`, `workflowdefinitions`, maybe `workflowapprovals`) and define how namespace/project scope is derived and persisted for each. If that mapping is not ready, defer the SAR backend and ship `PolicyFileAuthorizer` first.

### 4. Major: the Podman/shared-secret path does not satisfy the stated “per-user/team RBAC” requirement
The proposal says shared-secret mode maps all callers to `"anonymous"` and is suitable for single-team / Podman deployments, but that is not per-user or per-team RBAC. In that mode, all callers remain indistinguishable for trigger/approve/view/cancel, so the design only provides service-level authorization, not identity-based access control. That may be acceptable as a deployment limitation, but the document currently presents it as one of the primary T7 backends rather than as a reduced-capability fallback.

Recommended fix: make this limitation explicit in the design and acceptance criteria. State that shared-secret + policy mode is coarse deployment-level authorization only, not true per-user/team RBAC. If Podman needs real user/team separation, add a JWT/OIDC-backed identity mode rather than treating `"anonymous"` rules as equivalent.

## Perspective Check
- Functionality: major gaps remain around how later operations (`approve`, `view`, `cancel`) recover the scope and identity data needed for authorization.
- Quality: the design has internal contradictions (`identity extraction already done` vs implementation step 5) and an under-defined SAR resource model that does not cleanly match the real API.
- Security: the trust boundary is improved in intent, but the current design still lacks a server-defined identity contract and overstates what shared-secret mode can guarantee.

## Open Questions / Assumptions
- Is namespace/project scope supposed to come from workflow definitions, trigger requests, auth claims, or deployment config?
- Is SAR a Phase 2 requirement, or would a PolicyFile-first implementation satisfy T7 while the resource model is worked out?
- Should approval authorization depend only on caller identity, or also on the pending step’s stored `risk_level` and workflow ownership metadata?

## Summary
Not LGTM. The proposal has the right high-level direction, but it is missing two core runtime contracts: how caller identity actually reaches the API as structured data, and how workflow authorization context is persisted so later operations can be authorized correctly. The SAR backend and Podman/shared-secret story both need sharper scoping before this is implementation-ready.
