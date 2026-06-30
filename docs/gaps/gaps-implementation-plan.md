# Implementation Plan

Single source of truth for all planned work. ARCHITECTURE.md TODO tags link here.

Items are organized by area. Each has a status: **Open**, **Decided**, **Closed**, or **Done**.

---

## Ephemeral Execution (source: `ephemeral-execution-gaps.md`)

### T1: Forward PermissionScope to sandbox contract

**Status**: Open
**ARCHITECTURE.md ref**: Security TODO — per-step tool filtering

**Problem**: `allowed_tools` / `denied_tools` from `WorkflowStepSpec.permissions` are never passed to the sandbox. Per-step tool scoping is defined in the model but not enforced in the workflow path.

**What to build**:
1. In `temporal_activities.py`, extract `allowed_tools` and `denied_tools` from `step.permissions` and include them in the `request_body` sent to the sandbox.
2. Sandbox runtime must consume these fields — may require upstream contract extension.

**Tests**:
- Unit: `allowed_tools` in step permissions → appears in sandbox POST body
- Integration: workflow step with `denied_tools` → agent cannot call that tool

**Effort**: 1 day (activity side) + TBD (sandbox side)

**Decision needed**: Does the sandbox `/v1/agent/run` contract already support `allowedTools`/`deniedTools`?

### T2: Explicit sandbox termination on timeout/cancellation

**Status**: Open
**ARCHITECTURE.md ref**: Temporal Server — explicit sandbox termination on timeout

**Problem**: When a Temporal activity times out, cleanup is best-effort in `finally`. No heartbeat, no explicit kill signal if worker crashes mid-timeout.

**What to build**:
1. Add `activity.heartbeat()` during sandbox HTTP call
2. Distinguish cancellation from normal completion in `finally`
3. Add `ls_sandbox_timeout_total` counter

**Tests**:
- Unit: activity cancellation → destroy still called
- Unit: heartbeat called during sandbox HTTP request

**Effort**: 1 day

**Decision needed**: Is Temporal heartbeat sufficient, or do we need `spawner.terminate()` (SIGTERM before destroy)?

### T3: Cleanup failure metrics

**Status**: Open
**ARCHITECTURE.md ref**: Security TODO — cleanup failure metrics

**Problem**: Failed `spawner.destroy()` only logs a warning. Leaked containers invisible in dashboards.

**What to build**:
1. `ls_sandbox_cleanup_failures_total` Prometheus counter
2. `ls_sandbox_orphans_cleaned_total` counter for orphan reconciliation

**Effort**: Half day

---

## Sandbox Runtime (source: `sandbox-runtime-gaps.md`)

### T4: Unify runtime HTTP contract — CLOSED

Generic runtime removed. Only one contract exists: `POST /v1/agent/run`.

### T5: Document runtime input completeness

**Status**: Open
**ARCHITECTURE.md ref**: Sandbox Runtime config table

**What to build**: Doc-only. ARCHITECTURE.md Sandbox Runtime section already updated with MCP and provider env vars. Verify completeness.

**Effort**: Half day

### T6: Runtime convergence — RESOLVED

Decision: Option 3 (remove). Generic runtime was PoC1 legacy — removed.

---

## Security & Access Control

### T7: Per-user/team RBAC (R13)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R13 — TODO

**Problem**: Anyone with API access can trigger, approve, or view any workflow. No identity-based access control.

**What to build**:
- RBAC model: who can trigger, approve, view workflows
- Scoped by team, role, or namespace
- Enforcement at API layer

**Effort**: 1-2 weeks

### T8: Per-sandbox identity binding

**Status**: Open

**Problem**: Current TokenReview validates any `cloud-agents` audience token. Does not bind caller identity to the specific spawned sandbox container.

**What to build**: Generate scoped ServiceAccounts per sandbox spawn; verify identity matches the specific sandbox when results are returned.

**Effort**: 1-2 weeks

### T9: Dynamic RBAC from agent output

**Status**: Open (from operator comparison Gap 4)

**Problem**: Agent output can declare RBAC requirements, but the framework doesn't create scoped Roles/RoleBindings dynamically.

**What to build**: After analysis step, read `rbac` field from output, create per-proposal Roles/RoleBindings, clean up on completion.

**Effort**: 2-3 weeks

### ~~T10: Tool origin validation allowlist~~ — REMOVED

PoC1 leftover. Referenced `load_tools()` / `importlib.import_module()` from the deleted generic runtime. The Temporal workflow path does not load tool modules — tools are built into the sandbox image or provided via MCP servers.

---

## Triggers & Composition (R15, R16)

### T11: Agents-as-tools (R16)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R16 — TODO

**Problem**: No way for a chatbot conversation to invoke workflows as LLM tools.

**What to build**: Registry auto-generates LLM tool definitions from workflow definitions. Chatbot agent calls `start_diagnostic_workflow(cluster, issue)` as a tool.

**Effort**: 2-3 weeks. Depends on chatbot integration (T12).

### T12: Chatbot trigger (R15)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: Only API trigger exists. No chatbot/conversation trigger.

**What to build**: Integration with LCS `/query` conversation flow. Depends on T11 (agents-as-tools).

**Effort**: TBD — depends on LCS integration scope

### T13: Alert trigger (R15)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No Alertmanager webhook → workflow trigger.

**What to build**: Webhook endpoint that accepts Alertmanager payloads and starts workflows.

**Effort**: 1 week

### T14: Schedule trigger (R15)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No cron/scheduled workflow execution.

**What to build**: Expose Temporal's native cron schedule via API.

**Effort**: 2-3 days

---

## Escalation & Handoff (R17)

### T15: Interactive CLI handoff (R5, R17)

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R17 — TODO; Design Principle R5 — TODO

**Problem**: Escalation packages context but doesn't hand off to an interactive CLI session.

**What to build**: Escalation package → Claude Code / Goose session with pre-loaded workflow context (diagnosis, steps taken, failure history, tools, cluster state).

**Effort**: 1-2 weeks

### T16: Conversational approval

**Status**: Open (from BACKLOG.md)

**Problem**: Approval gates pause workflows but the only channels are Slack/webhook. No in-conversation approval.

**What to build**: When a workflow hits an approval gate, the LLM surfaces it to the user in natural language; user approves/rejects in the conversation flow.

**Effort**: TBD — depends on chatbot integration

---

## Agent Progress Streaming

### T36: Stream agent work-in-progress to callers

**Status**: Open
**ARCHITECTURE.md ref**: Observability; Sandbox Runtime

**Problem**: The workflow activity makes a synchronous HTTP call to the sandbox and waits for the final result. The sandbox streams internally from the LLM (the OpenAI agents SDK supports it) but collapses everything into a single response. Callers see only workflow-level events (step started/completed) via SSE — no token-by-token output, tool calls, or intermediate results from the agent.

**Architecture**:
```
User ← SSE ← Workflow Runner ← side channel ← Sandbox → LLM
                    ↕ gRPC
              Temporal (lifecycle only)
```

Temporal stays in control of lifecycle (start, timeout, retry, approval). The streaming data flows through a side channel, not through Temporal activities.

**What to build**:
1. **Sandbox → side channel**: The sandbox publishes progress events (LLM tokens, tool calls, intermediate results) to a side channel during execution. Options: Redis pubsub, SSE from sandbox directly, or a lightweight event bus. Events keyed by `(workflow_id, step_name)`.
2. **Activity registers the side channel**: When the activity spawns the sandbox, it passes a channel endpoint (e.g., callback URL or Redis topic) so the sandbox knows where to publish.
3. **Workflow runner SSE enrichment**: The existing `GET /v1/workflows/{id}/events` SSE endpoint subscribes to the side channel and forwards agent progress events alongside workflow-level events.
4. **Contract extension**: The sandbox `/v1/agent/run` contract gains an optional `progressEndpoint` or `progressChannel` field in the request body.

**What NOT to change**: The activity still makes a synchronous HTTP call for the final result. Temporal still controls retry/timeout. The streaming is a side channel, not a replacement for the activity return value.

**Decision needed**:
- Which side channel? Redis pubsub (requires Redis), direct SSE from sandbox to runner (simpler but requires network access), or shared volume with file-based events (no extra infra)?
- Should streaming be opt-in per workflow step, or always-on?

**Effort**: 1-2 weeks

---

## Operational Readiness

### T17: Prometheus alerting rules

**Status**: Open (from productization-roadmap.md P1)

**What to build**: PrometheusRule CRD with alerts for: step failure rate, orphaned pods, Temporal Worker heartbeat, LLM provider errors.

**Effort**: 1 day

### T18: Operational runbooks

**Status**: Open (from productization-roadmap.md P1)

**What to build**: `docs/operations/runbook.md` covering common failure modes and recovery.

**Effort**: 1 day

### T19: Circuit breaker for LLM provider

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Track recent failures per provider. After N consecutive failures, fail fast instead of spawning sandbox pods that will time out.

**Effort**: 1-2 days

### T20: Load and stress testing

**Status**: Open (from productization-roadmap.md P1)

**What to build**: `tests/load/` with concurrent workflow scenarios.

**Effort**: 2-3 days

### T21: Template interpolation sanitization

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Validate interpolated values don't contain template syntax (preventing recursive interpolation). Length-limit values.

**Effort**: Half day

### T22: Per-workflow model provider derivation

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Add `model_provider` field to `ProviderConfig`. Activity sets `LIGHTSPEED_MODEL_PROVIDER` from this field per workflow.

**Effort**: 1 day

### T23: Rate limiting

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Per-user request-level rate limiting. Existing spawner/worker concurrency caps handle pod storms; this adds API-level throttling for multi-tenant.

**Effort**: 2-3 days

### T24: Pod disruption budgets

**Status**: Open (from productization-roadmap.md P1)

**What to build**: PDB template in Helm chart: `minAvailable: 1` when replicas > 1.

**Effort**: Half day

---

## Workflow Features (from BACKLOG.md)

### T25: Nested workflows

**Status**: Open

Workflow-to-workflow composition (recursive execution).

### T26: Workflow versioning and rollback

**Status**: Open

Schema migration + state compatibility for definition updates.

### T27: Resumable SSE streaming

**Status**: Open

Persisted event replay via `Last-Event-ID`.

### ~~T28: Async callback dispatch~~ — REMOVED

PoC1 leftover. Described ephemeral pods POSTing results back to a runner ingest API. In the Temporal architecture, the activity calls the sandbox synchronously via HTTP and collects the result directly. No callback mechanism needed.

---

## Infrastructure (from BACKLOG.md)

### T29: Native K8s image volumes for skills

**Status**: Open (from operator comparison Gap 6)

K8s 1.31+ image volumes instead of init container. Fallback for older versions.

### T30: Spawner spec caching

**Status**: Open (from operator comparison Gap 7)

Cache spawner configurations (env vars, volumes, labels) by content hash. When multiple workflow steps use identical sandbox config, reuse the cached spec instead of rebuilding it. Low priority — negligible overhead at expected volumes.

### T31: Agent artifact storage

**Status**: Open

OCI artifacts, derived images, git-sync sidecar for tool/skill distribution.

### T32: Workflow visualization

**Status**: Open

Graph rendering UI or OpenShift console plugin integration.

### T33: SBOM / SLSA provenance

**Status**: Open

Image signing attestation and software bill of materials.

### T34: Multi-replica E2E testing

**Status**: Open

2-replica workflow runner deployment with Temporal. Test: start workflow on replica A, kill replica A, verify Temporal re-dispatches activities to replica B and workflow completes.

### T35: CRD-based K8s operator

**Status**: Open (from kubeclaw comparison)

Thin CRD-to-executor bridge for kubectl/GitOps workflows.
