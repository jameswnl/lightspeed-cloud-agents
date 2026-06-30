# Approval Routing Design

## Problem

When a workflow step requires human approval, the current system pauses and waits for any authenticated caller to POST to `/v1/workflows/{id}/approve`. There is no mechanism to:

1. **Route** the approval request to the right person or group
2. **Authorize** that the approver has permission for this specific action
3. **Audit** who approved, when, and what they reviewed

## Requirements

- Route approvals to specific users, groups, or roles based on the step's risk level and domain
- Support multiple notification/routing channels (Slack, email, webhook, in-app, conversational)
- Enforce that only authorized users can approve (not just anyone with an API token)
- Record a complete audit trail (who, when, what was shown, decision)
- Extensible via plugins — product teams integrate their own channels

## Proposed Design: Approval Router with Channel Plugins

```
workflow step pauses
        │
        v
  ApprovalRouter
        │
        ├── resolves: WHO should approve (from step config)
        ├── resolves: HOW to notify them (from channel plugins)
        └── enforces: only authorized approvers can resume
```

### Approval Spec in Workflow YAML

```yaml
steps:
  - name: approve-remediation
    type: human-approval
    message: "Review and approve the proposed remediation actions."
    output_key: approval
    approval:
      # WHO can approve
      approvers:
        users: ["sre-lead@redhat.com"]
        groups: ["platform-sre"]
        roles: ["workflow-approver"]
      
      # HOW to notify them
      channels:
        - type: slack
          config:
            webhook_url_env: SLACK_APPROVAL_WEBHOOK
            channel: "#sre-approvals"
        - type: email
          config:
            to: ["sre-oncall@redhat.com"]
            template: "approval-request"
        - type: webhook
          config:
            url: "https://approval-system.internal/api/request"
      
      # WHAT they see
      context_fields:
        - "steps.diagnosis.output.summary"
        - "steps.diagnosis.output.risk_level"
        - "steps.proposal.output.actions"
      
      # POLICY
      required_approvals: 1          # how many approvals needed
      timeout_seconds: 3600          # auto-reject after timeout
      escalation_after_seconds: 1800 # escalate if no response
```

### Channel Plugin Interface

```python
class ApprovalChannel(Protocol):
    """Plugin interface for approval notification channels."""

    async def send_request(
        self,
        request: ApprovalRequest,
    ) -> None:
        """Send an approval request notification."""
        ...

    async def check_response(
        self,
        request_id: str,
    ) -> ApprovalResponse | None:
        """Poll for a response (for channels that support callbacks)."""
        ...
```

**Built-in channels:**
- `SlackChannel` — sends Block Kit message with approve/reject buttons, receives callback
- `WebhookChannel` — POSTs request, receives callback at configured URL  
- `ConversationalChannel` — surfaces in the `/query` conversation flow (Phase 6)

**Product teams add their own:**
- `ServiceNowChannel` — creates approval record in ServiceNow
- `PagerDutyChannel` — pages the on-call approver
- `JiraChannel` — creates approval issue
- Custom internal approval systems

### ApprovalRequest Model

```python
class ApprovalRequest(BaseModel):
    """What the approver sees."""
    request_id: str
    workflow_id: str
    workflow_name: str
    step_name: str
    message: str
    context: dict[str, Any]          # rendered from context_fields
    proposed_actions: list[dict]     # what will happen if approved
    risk_level: str
    required_permissions: list[str]
    rollback_plan: str | None
    approve_url: str                 # callback URL
    reject_url: str
    expires_at: str                  # ISO timestamp
```

### ApprovalResponse Model

```python
class ApprovalResponse(BaseModel):
    """The approver's decision."""
    request_id: str
    approved: bool
    approver_id: str                # who approved (from identity provider)
    approver_email: str
    approver_groups: list[str]      # for RBAC validation
    reason: str | None              # optional explanation
    timestamp: str
```

### Authorization Flow

```
1. Workflow pauses at approval step
2. ApprovalRouter reads step.approval config
3. Router sends ApprovalRequest to all configured channels
4. Approver receives notification (Slack, email, etc.)
5. Approver clicks approve/reject (or responds in conversation)
6. Response arrives at /v1/workflows/{id}/approve with approver identity
7. Router validates:
   a. Is this approver in the allowed users/groups/roles?
   b. Has the request not expired?
   c. Is this the right workflow/step?
8. If valid: resume workflow with audit record
9. If invalid: reject with reason, do NOT resume
```

### Identity Integration

The approval system needs to know WHO the approver is. This plugs into lightspeed-stack's existing auth:

| Auth Method | How Approver Identity Works |
|------------|---------------------------|
| K8s TokenReview | User from ServiceAccount or OIDC token |
| JWK/OIDC | User claims from JWT (email, groups) |
| RH Identity | x-rh-identity header (org_id, user) |
| Noop (dev) | Trust the `approver_id` field in the request |

### Audit Trail

Every approval decision is persisted:

```python
class ApprovalAuditRecord(BaseModel):
    workflow_id: str
    step_name: str
    request_id: str
    decision: Literal["approved", "rejected", "expired", "escalated"]
    approver_id: str | None
    approver_groups: list[str]
    reason: str | None
    context_shown: dict[str, Any]    # what the approver saw
    timestamp: str
    response_time_seconds: float     # how long they took
```

Stored alongside workflow state in the persistence backend (PostgreSQL in production).

## Production Gaps (Current State)

| Gap | Current State | This Design |
|-----|--------------|-------------|
| Identity | Single shared bearer token | User identity from JWT/OIDC/K8s |
| Authorization | Anyone with token can approve | RBAC: users/groups/roles per step |
| Routing | No routing — caller must know the endpoint | Multi-channel notification plugins |
| Audit | No audit trail | Full audit record per decision |
| Escalation | Timeout → fail | Timeout → escalate to backup approvers |
| Multi-approval | Single approval | Configurable required_approvals count |

## Implementation Path

This belongs in **Phase 6** alongside:
- Conversational approval (approval surfaces through `/query` chat)
- pydantic-ai as core engine (user identity flows from conversation session)

### Dependencies
- Phase 6: pydantic-ai engine swap (for conversational channel)
- Existing: SlackNotifier (Phase 4c, can be extended to SlackChannel)
- Existing: WebhookNotifier (Phase 4c, can be extended to WebhookChannel)
- Existing: ApprovalPolicy auto-approve (stays — low-risk steps skip the router entirely)

### Suggested Phase 6 Tasks
1. `ApprovalChannel` protocol + built-in Slack/webhook channels
2. `ApprovalRouter` with RBAC validation
3. `ApprovalRequest`/`ApprovalResponse` models
4. Audit trail persistence
5. Wire into workflow executor approval step
6. Conversational channel (approval via `/query`)
