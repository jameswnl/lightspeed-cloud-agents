# T7: Per-User/Team RBAC Design

**Status**: Design proposal
**Requirement**: R13 — RBAC for workflows: who can trigger, approve, view. Scoped by team, role, or namespace.

## Problem

Currently, anyone with a valid bearer token (shared secret or SA token) can:
- Trigger any workflow (`POST /v1/workflows/run`)
- Approve/deny any step (`POST /v1/workflows/{id}/approve`)
- View any workflow status (`GET /v1/workflows/{id}`)
- Cancel any workflow (`POST /v1/workflows/{id}/cancel`)
- Submit/modify workflow definitions (`POST /v1/definitions`)

There's no identity-based access control. The auth layer validates *that* you have a token, not *what* you're allowed to do.

## How the Operator Does It

The lightspeed-agentic-operator relies entirely on Kubernetes RBAC:

1. **Trigger**: Creating a `Proposal` CR requires `create` permission on `proposals.agentic.openshift.io` — enforced by the K8s API server, not the operator.
2. **Approve**: Patching a `ProposalApproval` CR requires `update` permission. A mutating webhook stamps the `ApproverInfo` (UID, username, timestamp) from the AdmissionReview — the approver identity is server-authoritative, not client-supplied.
3. **View**: Reading Proposal status requires `get`/`list` permission on the CR, scoped by namespace RBAC.
4. **Policy**: The `ApprovalPolicy` CR (cluster-scoped singleton) controls which steps auto-approve vs require manual approval.

**Key insight**: The operator doesn't implement authorization logic — it delegates entirely to the K8s API server's RBAC. Identity comes from the K8s user (via certificate, token, or OIDC), and permissions come from Role/ClusterRole bindings.

## Why Cloud Agents Can't Just Copy This

Cloud Agents is an HTTP service, not a Kubernetes controller. Its API is FastAPI endpoints, not CRD mutations. The K8s API server isn't in the request path for workflow operations.

However, Cloud Agents **can** use K8s RBAC as a backend for authorization decisions when running on K8s, while providing an alternative for Podman deployments.

## Design

### Layer 1: Identity Extraction (already done)

The current auth middleware extracts the caller's identity:
- **Shared secret mode**: All callers share the same token. No individual identity. Suitable for single-team / Podman deployments.
- **SA token mode**: K8s TokenReview validates the token and returns the authenticated user (ServiceAccount name + namespace). This gives us per-caller identity.

**No changes needed here.** The auth middleware stays as-is. We add an authorization layer on top.

### Layer 2: Authorization (new)

A new `WorkflowAuthorizer` that checks whether the authenticated identity is allowed to perform the requested action.

```python
class WorkflowAction(str, Enum):
    """Actions that can be authorized on workflow resources."""
    TRIGGER = "trigger"      # Start a workflow
    APPROVE = "approve"      # Approve/deny a step
    VIEW = "view"            # View workflow status/events
    CANCEL = "cancel"        # Cancel a workflow
    MANAGE_DEFS = "manage"   # Submit/modify definitions
```

```python
class WorkflowAuthorizer(ABC):
    """Decides whether a caller can perform an action on a workflow."""

    @abstractmethod
    async def authorize(
        self,
        identity: CallerIdentity,
        action: WorkflowAction,
        resource: WorkflowResource,
    ) -> AuthzDecision:
        """Check if identity can perform action on resource.

        Returns AuthzDecision with allowed=True/False and reason.
        """
        ...
```

### Implementation: Three backends

#### Backend 1: `NoopAuthorizer` (default, backward-compatible)

All actions allowed. No authorization checks. This is the current behavior.

```python
class NoopAuthorizer(WorkflowAuthorizer):
    async def authorize(self, identity, action, resource):
        return AuthzDecision(allowed=True, reason="authorization disabled")
```

Used when: `WORKFLOW_AUTHZ=none` (default) or Podman deployments with shared secret auth.

#### Backend 2: `PolicyFileAuthorizer` (static rules, any deployment)

Authorization rules defined in a YAML policy file, loaded at startup. Works on both K8s and Podman.

```yaml
# workflow-policy.yaml
rules:
  - identity: "team:sre"
    actions: [trigger, approve, cancel, view, manage]
    workflows: ["*"]

  - identity: "team:developers"
    actions: [trigger, view]
    workflows: ["diagnose-*"]

  - identity: "user:oncall-bot"
    actions: [trigger]
    workflows: ["alert-triage"]

  - identity: "team:platform"
    actions: [approve]
    workflows: ["*"]
    conditions:
      risk_levels: [high, critical]  # can only approve high-risk steps

defaults:
  # Actions allowed for any authenticated user
  allow: [view]
  # Actions that require explicit rule match
  deny_unless_matched: [trigger, approve, cancel, manage]
```

```python
class PolicyFileAuthorizer(WorkflowAuthorizer):
    def __init__(self, policy_path: str):
        self.rules = load_policy(policy_path)

    async def authorize(self, identity, action, resource):
        # Check explicit rules first
        for rule in self.rules:
            if matches(rule, identity, action, resource):
                return AuthzDecision(allowed=True, reason=f"rule: {rule.identity}")

        # Check defaults
        if action in self.defaults.allow:
            return AuthzDecision(allowed=True, reason="default allow")

        return AuthzDecision(allowed=False, reason="no matching rule")
```

Used when: `WORKFLOW_AUTHZ=policy` + `WORKFLOW_AUTHZ_POLICY_PATH=/etc/cloud-agents/policy.yaml`

**Identity matching**: The `identity` field in rules matches against the caller's identity string. How the identity string is constructed depends on the auth mode:

- **SA token mode**: Identity = `"sa:{namespace}:{service-account-name}"` (from TokenReview response)
- **JWT mode**: Identity = `"user:{username}"` or `"team:{group}"` (from JWT claims)
- **Shared secret mode**: Identity = `"anonymous"` (no individual identity — all callers match the same rules)

#### Backend 3: `K8sSubjectAccessReview` (K8s-native, OCP)

Delegates authorization to the K8s API server via SubjectAccessReview. Uses the caller's token to ask "can this user perform this action on this resource?"

```python
class K8sSARAuthorizer(WorkflowAuthorizer):
    """Delegates to K8s SubjectAccessReview for authorization decisions."""

    RESOURCE_GROUP = "cloud-agents.lightspeed.redhat.com"

    async def authorize(self, identity, action, resource):
        sar = V1SubjectAccessReview(
            spec=V1SubjectAccessReviewSpec(
                user=identity.username,
                groups=identity.groups,
                resource_attributes=V1ResourceAttributes(
                    namespace=resource.namespace,
                    verb=self._action_to_verb(action),
                    group=self.RESOURCE_GROUP,
                    resource="workflows",
                    name=resource.workflow_name,
                ),
            ),
        )
        result = await auth_api.create_subject_access_review(sar)
        return AuthzDecision(
            allowed=result.status.allowed,
            reason=result.status.reason or "SAR",
        )

    def _action_to_verb(self, action: WorkflowAction) -> str:
        return {
            WorkflowAction.TRIGGER: "create",
            WorkflowAction.APPROVE: "update",
            WorkflowAction.VIEW: "get",
            WorkflowAction.CANCEL: "delete",
            WorkflowAction.MANAGE_DEFS: "create",
        }[action]
```

This requires corresponding RBAC resources on the cluster:

```yaml
# ClusterRole for SRE team
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cloud-agents-admin
rules:
  - apiGroups: ["cloud-agents.lightspeed.redhat.com"]
    resources: ["workflows"]
    verbs: ["create", "get", "list", "update", "delete"]

# ClusterRole for viewers
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cloud-agents-viewer
rules:
  - apiGroups: ["cloud-agents.lightspeed.redhat.com"]
    resources: ["workflows"]
    verbs: ["get", "list"]
```

**Note**: This doesn't require CRDs. SAR checks authorization against RBAC rules for arbitrary API groups/resources — even ones that don't exist as CRDs. The K8s API server evaluates the RBAC rules without checking if the resource is registered.

Used when: `WORKFLOW_AUTHZ=k8s-sar` (requires K8s cluster with RBAC configured).

### Layer 3: Approval Identity Capture

When a user approves/denies a step, capture their identity in the approval record. The operator does this with a mutating webhook (`ApproverInfo`). Cloud Agents does it in the API handler:

```python
@router.post("/{workflow_id}/approve")
async def approve_workflow(
    workflow_id: str,
    request: ApproveRequest,
    caller: CallerIdentity = Depends(get_caller_identity),
):
    # Authorize
    decision = await authorizer.authorize(
        caller,
        WorkflowAction.APPROVE,
        WorkflowResource(workflow_id=workflow_id, step=request.step_name),
    )
    if not decision.allowed:
        raise HTTPException(403, f"Not authorized to approve: {decision.reason}")

    # Capture approver identity in the signal
    await handle.signal(
        AgentWorkflow.approve,
        request.step_name,
        request.decision,
        request.selected_option_id,
        # Approver identity recorded in workflow state
        approver=ApproverInfo(
            username=caller.username,
            uid=caller.uid,
            approved_at=datetime.utcnow().isoformat(),
        ),
    )

    emit_audit("workflow.approved", ...)
```

The `ApproverInfo` is stored in the workflow's step result, queryable via `GET /v1/workflows/{id}`. This provides the audit trail that the operator gets from the `ApproverInfo` on the ProposalApproval CR.

### Wiring Into the API

The authorizer is injected into `build_temporal_router` alongside the existing auth dependency:

```python
def build_temporal_router(
    temporal_client: Client,
    auth_dependency: Optional[Any] = None,
    authorizer: Optional[WorkflowAuthorizer] = None,  # NEW
    definition_store: Optional[DefinitionStore] = None,
) -> APIRouter:
    authz = authorizer or NoopAuthorizer()

    @router.post("/run", ...)
    async def run_workflow(
        request: RunWorkflowRequest,
        caller: CallerIdentity = Depends(get_caller_identity),
    ):
        decision = await authz.authorize(
            caller, WorkflowAction.TRIGGER,
            WorkflowResource(workflow_name=request.workflow_name),
        )
        if not decision.allowed:
            raise HTTPException(403, decision.reason)
        # ... start workflow
```

### Configuration

```
# Environment variables
WORKFLOW_AUTHZ=none|policy|k8s-sar     # Authorization backend (default: none)
WORKFLOW_AUTHZ_POLICY_PATH=/path       # For policy backend
```

For Helm chart:
```yaml
# values.yaml
authorization:
  mode: policy  # none | policy | k8s-sar
  policyFile: /etc/cloud-agents/policy.yaml
```

## Comparison with Operator Approach

| Concern | Operator | Cloud Agents |
|---|---|---|
| **Identity source** | K8s user (cert/OIDC/token) | SA token (K8s) or JWT or shared secret |
| **Authorization engine** | K8s API server RBAC | Pluggable: Noop / PolicyFile / K8s SAR |
| **Trigger control** | RBAC on `proposals.create` | Authorizer checks `WorkflowAction.TRIGGER` |
| **Approval identity** | Mutating webhook stamps `ApproverInfo` | API handler captures `CallerIdentity` |
| **Namespace scoping** | Native K8s namespace RBAC | PolicyFile rules with workflow name patterns, or SAR with namespace |
| **Podman support** | N/A (K8s only) | PolicyFile backend works without K8s |

## What the Operator Gets That We Don't (and Why That's OK)

1. **Mutating webhook for approver identity** — The operator's webhook is server-authoritative; clients can't forge the approver. Cloud Agents captures identity in the API handler, which is trust-equivalent for an HTTP service (the server controls the code path).

2. **Namespace-level isolation via K8s RBAC** — The operator naturally inherits namespace scoping because Proposals are namespaced CRDs. Cloud Agents workflows are Temporal executions, not K8s resources. Namespace scoping is done via policy rules or SAR checks, not native K8s namespace RBAC. This is less granular but works across K8s and Podman.

3. **Per-step approval RBAC** — The operator's ProposalApproval CR allows different users to approve different steps. Cloud Agents can do this with PolicyFile rules that include `conditions.risk_levels` — a user can be authorized to approve low-risk steps but not high-risk ones.

## Implementation Plan

| Step | What | Effort |
|---|---|---|
| 1 | Define `WorkflowAction`, `CallerIdentity`, `WorkflowResource`, `AuthzDecision` models | 2 hours |
| 2 | Implement `WorkflowAuthorizer` ABC + `NoopAuthorizer` | 1 hour |
| 3 | Implement `PolicyFileAuthorizer` with YAML loading + rule matching | 1 day |
| 4 | Wire authorizer into `build_temporal_router` — add authorization checks to all endpoints | 1 day |
| 5 | Implement `CallerIdentity` extraction from auth context (SA token → username, JWT → claims) | Half day |
| 6 | Add `ApproverInfo` to approval signal + workflow state | Half day |
| 7 | Implement `K8sSARAuthorizer` | 1 day |
| 8 | Add RBAC ClusterRole examples to Helm chart | Half day |
| 9 | Tests: unit (all 3 backends) + integration (policy rules) + E2E (K8s SAR on Kind) | 2 days |
| **Total** | | **~6-7 days** |

## Tests

```
tests/unit/agents/workflow/test_authorizer.py:
  TestNoopAuthorizer:
    test_allows_everything

  TestPolicyFileAuthorizer:
    test_explicit_allow_rule
    test_explicit_deny_no_match
    test_wildcard_workflow_match
    test_default_allow_view
    test_default_deny_trigger
    test_risk_level_condition_on_approve
    test_identity_matching_sa_token
    test_identity_matching_jwt_user
    test_identity_matching_anonymous

  TestK8sSARAuthorizer:
    test_allowed_by_sar
    test_denied_by_sar
    test_action_to_verb_mapping

  TestCallerIdentity:
    test_from_sa_token_review
    test_from_jwt_claims
    test_anonymous_fallback

tests/e2e/temporal/test_temporal_rbac.py:
  test_unauthorized_trigger_returns_403
  test_authorized_trigger_succeeds
  test_unauthorized_approve_returns_403
  test_approver_identity_recorded_in_state
  test_policy_file_loaded_from_config
  test_k8s_sar_on_kind_cluster
```
