# Implementation Plan

Single source of truth for all planned work. ARCHITECTURE.md TODO tags link here.

Items are organized by area. Each has a status: **Open**, **Decided**, **Closed**, or **Done**.

## Priority Phases

| Phase | Focus | Tasks |
|-------|-------|-------|
| **Phase 1** | High value, enables other work | T1, T3, T22, T36 |
| **Phase 2** | Production hardening | T7, T17, T19, T21, T24 |
| **Phase 3** | Strategic / longer-term | T2, T8, T11, T13, T14, T15, T23 |
| **Phase 4** | Backlog | T5, T9, T12, T16, T18, T20, T25-T35 |
| **Unphased** | Security & governance (needs prioritization) | T37-T43 |

---

## Ephemeral Execution (source: `ephemeral-execution-gaps.md`)

### T1: Forward PermissionScope to sandbox contract [Phase 1] — DONE

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

### T2: Explicit sandbox termination on timeout/cancellation [Phase 3]

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

**Note**: T36 (async streaming) may change the sync HTTP model this task builds on. If T36 is implemented first, T2's heartbeat design should adapt to the streaming architecture. Both tasks add metrics to `temporal_metrics.py` — coordinate with T3.

### T3: Cleanup failure metrics [Phase 1] — DONE

**Status**: Open
**ARCHITECTURE.md ref**: Observability — cleanup failure metrics

**Problem**: Failed `spawner.destroy()` only logs a warning. Leaked containers invisible in dashboards.

**What to build**:
1. `ls_sandbox_cleanup_failures_total` Prometheus counter
2. `ls_sandbox_orphans_cleaned_total` counter for orphan reconciliation

**Effort**: Half day

---

## Sandbox Runtime (source: `sandbox-runtime-gaps.md`)

### T5: Document runtime input completeness [Phase 4]

**Status**: Nearly done — `LIGHTSPEED_SERVICE_ACCOUNT` missing from config table
**ARCHITECTURE.md ref**: Sandbox Runtime config table

**What to build**: Add `LIGHTSPEED_SERVICE_ACCOUNT` to the config table. Verify no other env vars are missing.

**Effort**: 15 minutes

---

## Security & Access Control

### T7: Per-user/team RBAC (R13) [Phase 2] — DONE

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R13 — TODO

**Problem**: Anyone with API access can trigger, approve, or view any workflow. No identity-based access control.

**What to build**:
- RBAC model: who can trigger, approve, view workflows
- Scoped by team, role, or namespace
- Enforcement at API layer

**Effort**: 1-2 weeks

### T8: Per-sandbox identity binding [Phase 3]

**Status**: Open

**Problem**: Current TokenReview validates any `cloud-agents` audience token. Does not bind caller identity to the specific spawned sandbox container.

**What to build**: Generate scoped ServiceAccounts per sandbox spawn; verify identity matches the specific sandbox when results are returned.

**Effort**: 1-2 weeks

### T9: Dynamic RBAC from agent output [Phase 4]

**Status**: Open (from operator comparison Gap 4)

**Problem**: Agent output can declare RBAC requirements, but the framework doesn't create scoped Roles/RoleBindings dynamically.

**What to build**: After analysis step, read `rbac` field from output, create per-proposal Roles/RoleBindings, clean up on completion.

**Effort**: 2-3 weeks

---

## Triggers & Composition (R15, R16)

### T11: Agents-as-tools (R16) [Phase 3]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R16 — TODO

**Problem**: No way for a chatbot conversation to invoke workflows as LLM tools.

**What to build**: Registry auto-generates LLM tool definitions from workflow definitions. Chatbot agent calls `start_diagnostic_workflow(cluster, issue)` as a tool.

**Effort**: 2-3 weeks. T12 (chatbot trigger) depends on this — not the other way around.

### T12: Chatbot trigger (R15) [Phase 4]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: Only API trigger exists. No chatbot/conversation trigger.

**What to build**: Integration with LCS `/query` conversation flow. Depends on T11 (agents-as-tools).

**Effort**: TBD — depends on LCS integration scope

### T13: Alert trigger (R15) [Phase 3]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No Alertmanager webhook → workflow trigger.

**What to build**: Webhook endpoint that accepts Alertmanager payloads and starts workflows.

**Effort**: 1 week

### T14: Schedule trigger (R15) [Phase 3]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No cron/scheduled workflow execution.

**What to build**: Expose Temporal's native cron schedule via API.

**Effort**: 2-3 days

---

## Escalation & Handoff (R17)

### T15: Interactive CLI handoff (R5, R17) [Phase 3]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R17 — TODO; Design Principle R5 — TODO

**Problem**: Escalation packages context but doesn't hand off to an interactive CLI session.

**What to build**: Escalation package → Claude Code / Goose session with pre-loaded workflow context (diagnosis, steps taken, failure history, tools, cluster state).

**Effort**: 1-2 weeks

### T16: Conversational approval [Phase 4]

**Status**: Open (from BACKLOG.md)

**Problem**: Approval gates pause workflows but the only channels are Slack/webhook. No in-conversation approval.

**What to build**: When a workflow hits an approval gate, the LLM surfaces it to the user in natural language; user approves/rejects in the conversation flow.

**Effort**: TBD — depends on chatbot integration

---

## Agent Progress Streaming

### T36: Stream agent work-in-progress to callers [Phase 1]

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

**Status**: Deferred — needs architecture design discussion before implementation. Detailed design draft and reviewer feedback captured below.

**Decisions needed**:
- Which side channel? Redis pubsub (requires Redis), direct SSE from sandbox to runner (simpler but requires network access), or shared volume with file-based events (no extra infra)?
- Should streaming be opt-in per workflow step, or always-on?
- How to handle multi-replica deployments? (see reviewer finding 1 below)

**Effort**: 1-2 weeks

### T36 design draft (from Phase 1 planning)

**Recommended approach**: Option A — direct callback from sandbox to runner. The sandbox POSTs progress events to a `progressUrl` provided in the request body.

**Callback addressing**: Configured via `WORKFLOW_RUNNER_CALLBACK_URL` env var. Must be routable from inside the spawned sandbox container (not `localhost`):
- K8s: `http://workflow-runner.{namespace}.svc:8080`
- Podman: `http://workflow-runner:8080` (shared network)
- Dev (Podman): `http://host.containers.internal:8080`

If not set, progress streaming is disabled (opt-in).

**Callback authentication**: Per-step callback token (random UUID):
1. Activity generates token at spawn time, passes as `progressToken` in request body
2. Sandbox sends `Authorization: Bearer {progressToken}` on every progress POST
3. Runner validates token + workflow_id + step_name on ingestion, rejects invalid/missing

**Event identity**: Keyed by `(workflow_id, step_name, attempt)` — not just workflow_id. Handles parallel steps and retries. Cleanup per-key when activity completes.

**ProgressStore**: In-memory buffer in the runner process. Single-replica only in initial implementation. Multi-replica (Redis or external event store) deferred.

**Runner-side components**:
- `progress_store.py` (new) — in-memory buffer with append/read_since/cleanup/register_token/validate_token
- `POST /v1/workflows/{id}/steps/{step}/progress` — authenticated ingestion endpoint
- SSE enrichment — existing events endpoint interleaves progress events

**Sandbox-side contract** (upstream work):
- Read `progressUrl` and `progressToken` from request body
- During LLM streaming: POST `{"type": "llm_token", "text": "..."}` with Bearer token
- During tool calls: POST `{"type": "tool_call", "name": "...", "input": "..."}` with Bearer token
- On tool result: POST `{"type": "tool_result", "name": "...", "output": "..."}` with Bearer token
- Include `attempt` number in every event for retry isolation

### T36 reviewer feedback (2 review rounds)

**Round 1 findings** (all addressed in design draft above):
1. In-memory ProgressStore breaks stateless runner contract → scoped as single-replica stepping stone
2. Progress endpoint unauthenticated → per-step Bearer token
3. Events keyed by workflow_id only → keyed by (workflow_id, step_name, attempt)
4. T1 forwarding-only doesn't close security gap → explicitly scoped, parent task stays open
5. progressUrl addressing undefined across deployment targets → WORKFLOW_RUNNER_CALLBACK_URL env var

**Round 2 findings** (addressed):
1. Sandbox-side section omitted auth contract → updated with Bearer token requirement
2. Dev callback example used `localhost` (unreachable from container) → replaced with `host.containers.internal`

**Open architecture questions** (to resolve before implementation):
1. Is single-replica ProgressStore acceptable for initial deployment, or must we start with Redis?
2. Should the sandbox-side work happen in the same phase, or is it a separate upstream task?
3. What event schema should progress events follow? (freeform dict, or a defined Pydantic model?)
4. Should progress events be persisted (for replay after reconnect), or ephemeral (lost on disconnect)?

---

## Operational Readiness

### T17: Prometheus alerting rules [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: PrometheusRule CRD with alerts for: step failure rate, orphaned pods, Temporal Worker heartbeat, LLM provider errors.

**Effort**: 1 day

### T18: Operational runbooks [Phase 4]

**Status**: Open (from productization-roadmap.md P1)

**What to build**: `docs/operations/runbook.md` covering common failure modes and recovery.

**Effort**: 1 day

### T19: Circuit breaker for LLM provider [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Track recent failures per provider. After N consecutive failures, fail fast instead of spawning sandbox pods that will time out.

**Effort**: 1-2 days

### T20: Load and stress testing [Phase 4]

**Status**: Open (from productization-roadmap.md P1)

**What to build**: `tests/load/` with concurrent workflow scenarios.

**Effort**: 2-3 days

### T21: Template interpolation sanitization [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Validate interpolated values don't contain template syntax (preventing recursive interpolation). Length-limit values.

**Effort**: Half day

### T22: Per-workflow model provider derivation [Phase 1] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Add `model_provider` field to `ProviderConfig`. Activity sets `LIGHTSPEED_MODEL_PROVIDER` from this field per workflow.

**Effort**: 1 day

### T23: Rate limiting [Phase 3]

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Per-user request-level rate limiting. Existing spawner/worker concurrency caps handle pod storms; this adds API-level throttling for multi-tenant.

**Effort**: 2-3 days

### T24: Pod disruption budgets [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: PDB template in Helm chart: `minAvailable: 1` when replicas > 1.

**Effort**: Half day

---

## Workflow Features (from BACKLOG.md)

### T25: Nested workflows [Phase 4]

**Status**: Open

Workflow-to-workflow composition (recursive execution).

### T26: Workflow versioning and rollback [Phase 4]

**Status**: Open

Schema migration + state compatibility for definition updates.

### T27: Resumable SSE streaming [Phase 4]

**Status**: Open. Depends on T36 (progress streaming enriches what's streamed; T27 adds reconnection).

Persisted event replay via `Last-Event-ID`.

---

## Infrastructure (from BACKLOG.md)

### T29: Native K8s image volumes for skills [Phase 4]

**Status**: Open (from operator comparison Gap 6)

K8s 1.31+ image volumes instead of init container. Fallback for older versions.

### T30: Spawner spec caching [Phase 4]

**Status**: Open (from operator comparison Gap 7)

Cache spawner configurations (env vars, volumes, labels) by content hash. When multiple workflow steps use identical sandbox config, reuse the cached spec instead of rebuilding it. Low priority — negligible overhead at expected volumes.

### T31: Agent artifact storage [Phase 4]

**Status**: Open

OCI artifacts, derived images, git-sync sidecar for tool/skill distribution.

### T32: Workflow visualization [Phase 4]

**Status**: Open

Graph rendering UI or OpenShift console plugin integration.

### T33: SBOM / SLSA provenance [Phase 4]

**Status**: Open

Image signing attestation and software bill of materials.

### T34: Multi-replica E2E testing [Phase 4]

**Status**: Open

2-replica workflow runner deployment with Temporal. Test: start workflow on replica A, kill replica A, verify Temporal re-dispatches activities to replica B and workflow completes.

### T35: CRD-based K8s operator [Phase 4]

**Status**: Open (from kubeclaw comparison)

Thin CRD-to-executor bridge for kubectl/GitOps workflows.

---

## Security & Governance Hardening

### T37: Secret redaction in logs and error responses [Unphased]

**Status**: Open

**Problem**: `credentials_secret` value is injected as a plain env var on sandbox pods. If a sandbox error response includes environment details or the activity logs the full env dict, secrets leak into logs or API responses. Audit events include `secret_name` but the activity doesn't redact credential values from error paths.

**What to build**:
- Redact known secret env var values from error responses before returning to callers
- Redact secret values from log messages in the activity (never log `env_vars` dict raw)
- Add a test that triggers an error path and asserts no secret values appear in the response or logs

**Effort**: 1 day

### T38: Request body size limits [Unphased]

**Status**: Open

**Problem**: `POST /v1/workflows/run` accepts arbitrarily large definition/prompt payloads. A malicious or misconfigured client could submit a multi-MB definition to exhaust memory or Temporal payload limits.

**What to build**:
- FastAPI request body size limit (configurable via `MAX_REQUEST_BODY_BYTES`, default 1MB)
- Return 413 Payload Too Large when exceeded
- Add test that verifies oversized payload is rejected

**Effort**: Half day

### T39: Sandbox network egress enforcement by default [Unphased]

**Status**: Open

**Problem**: Sandbox containers can make outbound requests to any endpoint, not just the LLM provider. NetworkPolicy exists in Helm but is opt-in (`networkPolicy.egress.enabled: false`). A compromised or malicious agent could exfiltrate data to arbitrary hosts.

**What to build**:
- Change Helm default to `networkPolicy.egress.enabled: true`
- Require explicit `llmCidrs` configuration for LLM provider access
- Document the egress policy in DEMO.md and rbac.md
- Add a note that Podman deployments need host firewall rules for equivalent protection

**Effort**: Half day (Helm change + docs)

### T40: Prompt injection guardrails [Unphased]

**Status**: Open

**Problem**: Workflow definitions are submitted as arbitrary dicts. Pydantic validates schema but doesn't restrict prompt/instruction content. A malicious prompt could instruct the LLM to exfiltrate data, ignore safety guidelines, or produce harmful output. This is especially relevant when non-admin users can trigger workflows (post-RBAC).

**What to build**:
- Design decision needed: input-side filtering (reject suspicious prompts) vs output-side filtering (scan agent output) vs both
- Consider integration with existing guardrail frameworks (pydantic-ai-shields, llm-guard)
- At minimum: log a warning when prompts contain known injection patterns (e.g., "ignore previous instructions", "system prompt override")

**Effort**: TBD — needs design discussion. Logging-only detection is 1-2 days; full guardrail integration is 1-2 weeks.

### T41: Audit log integrity [Unphased]

**Status**: Open

**Problem**: Audit events go to stdout/stderr via structured logging. No signed audit trail, no tamper-evident log chain, no guaranteed delivery. An operator with log access could modify audit records.

**What to build**:
- Append audit events to a dedicated audit log file (separate from application logs)
- Add HMAC signatures or hash chain for tamper detection
- Optionally: forward audit events to an external audit service (webhook)

**Effort**: 1-2 days for file-based audit log; 1 week for signed/chained logs

### T42: Token rotation and expiry for bearer auth [Unphased]

**Status**: Open

**Problem**: Bearer tokens are static (`AGENT_API_TOKEN` env var). No rotation mechanism, no expiry. A leaked token grants permanent access until the env var is manually changed and the runner restarted.

**What to build**:
- Support multiple valid tokens (`AGENT_API_TOKENS` comma-separated) for rotation
- Optional token expiry checking (if tokens include a timestamp or JWT-like structure)
- Log when a token is rejected to help operators detect leaked token usage

**Effort**: 1 day for multi-token support; 1 week for JWT-based expiry

### T43: Workflow definition content policy [Unphased]

**Status**: Open

**Problem**: RBAC controls who can submit definitions, but not what definitions contain. A user with `manage_defs` permission could submit a definition with instructions that bypass organizational policies (e.g., "ignore safety guidelines", "access all namespaces").

**What to build**:
- Definition content policy: configurable rules that validate definition content at submission time
- Examples: max prompt length, blocked instruction patterns, required output_schema fields, namespace restrictions
- Policy violations return 422 with details

**Effort**: 1-2 days for basic content rules; 1 week for configurable policy engine

---

## Closed / Removed

### T4: Unify runtime HTTP contract — CLOSED

Generic runtime removed. Only one contract exists: `POST /v1/agent/run`.

### T6: Runtime convergence — RESOLVED

Decision: Option 3 (remove). Generic runtime was PoC1 legacy — removed.

### T10: Tool origin validation allowlist — REMOVED

PoC1 leftover. The Temporal workflow path does not load tool modules — tools are built into the sandbox image or provided via MCP servers.

### T28: Async callback dispatch — REMOVED

PoC1 leftover. In the Temporal architecture, the activity calls the sandbox synchronously via HTTP. No callback mechanism needed.
