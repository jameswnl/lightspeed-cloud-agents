# Implementation Plan

Single source of truth for all planned work. ARCHITECTURE.md TODO tags link here.

Items are organized by area. Each has a status: **Open**, **Decided**, **Closed**, or **Done**.

## Priority Phases

| Phase | Focus | Tasks |
|-------|-------|-------|
| **Phase 1** | High value, enables other work | T1 ✓, T3 ✓, T22 ✓ |
| **Phase 2** | Production hardening | T7 ✓, T17 ✓, T19 ✓, T21 ✓, T24 ✓ |
| **Phase 3a** | Security quick wins | T37 ✓, T38 ✓, T39 ✓, T42 ✓, T43 ✓, T48 ✓ |
| **Phase 3b** | Triggers + hardening | T2 ✓, T13 ✓, T14 ✓, T23 ✓, T49 ✓, T50 ✓ |
| **Phase 4** | Strategic (needs design first) | T8, T11, T15, T36, T51 ✓ |
| **Phase 5** | Backlog | T5, T9, T12, T16, T18, T20, T25-T27, T29-T35, T40, T41 |

### Immediate actions (before Phase 3a)
1. **Pin Temporal SDK version** — `temporalio>=1.9.0` has no upper bound; add `<2.0` cap
2. **Add advisory tool enforcement for T1** — runner-side response validation as interim while waiting on sandbox upstream

### Phase 3/4 prerequisites (design before building)
3. **Auth evolution design doc** — unified design for T8, T36, T42 before any are implemented
4. **Sandbox team alignment on T36 contract** — longest lead-time cross-team dependency
5. **T11 scope narrowing** — solve sync/async for one workflow before auto-generation
6. **T15 scope narrowing** — "generate launch command" (days) not "launch session" (weeks)
7. **T2/T36 ordering** — resolve before implementing either
8. **T8/T9: accept K8s-only** — no Podman equivalent for SA identity/dynamic RBAC

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

### T2: Explicit sandbox termination on timeout/cancellation [Phase 3b] — DONE

**Status**: Done
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

**⚠ ORDERING CONFLICT with T36**: T36 may change the sync HTTP model this task builds on. Resolve ordering before implementing either. If T36 ships first, T2's heartbeat design should adapt to the streaming side channel.

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

### T8: Per-sandbox identity binding [Phase 4]

**Status**: Open

**Problem**: Current TokenReview validates any `cloud-agents` audience token. Does not bind caller identity to the specific spawned sandbox container.

**What to build**: Generate scoped ServiceAccounts per sandbox spawn; verify identity matches the specific sandbox when results are returned.

**Effort**: 3-4 weeks (revised up from 1-2 weeks)

**⚠ BLOCKER RISKS**:
- K8s SA lifecycle: 2 extra creates + 2 extra deletes per step; multiplies with parallel workflows
- Cleanup on failure: SA + RoleBinding orphaned if Job creation fails. Current orphan reconciliation only finds Jobs, not RBAC resources — needs extension.
- Token propagation timing: projected SA tokens need pod running before token available
- **K8s-only**: No Podman equivalent — accept feature divergence
- **Prerequisite**: Auth evolution design doc (shared with T36, T42)

### T9: Dynamic RBAC from agent output [Phase 5]

**Status**: Open (from operator comparison Gap 4)

**Problem**: Agent output can declare RBAC requirements, but the framework doesn't create scoped Roles/RoleBindings dynamically.

**What to build**: After analysis step, read `rbac` field from output, create per-proposal Roles/RoleBindings, clean up on completion.

**Effort**: 4-6 weeks (revised up from 2-3 weeks)

**⚠ BLOCKER RISKS**:
- Requires controller-like lifecycle management without K8s controller machinery
- Runner needs elevated RBAC (create Roles/RoleBindings) — expands security surface
- Cleanup depends on `finally` blocks which have known reliability issues
- Sandbox response schema has no `rbac` field yet
- **K8s-only**: No Podman equivalent
- **Prerequisite**: T8 (per-sandbox identity) should be done first

---

## Triggers & Composition (R15, R16)

### T11: Agents-as-tools (R16) [Phase 4]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R16 — TODO

**Problem**: No way for a chatbot conversation to invoke workflows as LLM tools.

**What to build**: Registry auto-generates LLM tool definitions from workflow definitions. Chatbot agent calls `start_diagnostic_workflow(cluster, issue)` as a tool.

**Effort**: 4-6 weeks (revised up from 2-3 weeks)

**⚠ BLOCKER RISKS**:
- **Sync/async impedance mismatch**: LLM tool calls expect synchronous responses. Temporal workflows are async (approvals, retries, minutes-long). Options: block (ties up LLM context), poll (breaks tool patterns), restrict to fast auto-approved workflows only. None solved yet.
- **Schema translation**: WorkflowInput has 13 fields — which become tool parameters? Mapping is non-obvious and different per workflow.
- **No consumer without T12**: T12 depends on T11, but T11 needs T12 to be useful. T12 depends on LCS integration (external team).
- **Scope recommendation**: Start with manually-registered tools for one specific workflow, not auto-generation. Solve the async pattern first.

### T12: Chatbot trigger (R15) [Phase 5]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: Only API trigger exists. No chatbot/conversation trigger.

**What to build**: Integration with LCS `/query` conversation flow. Depends on T11 (agents-as-tools).

**Effort**: TBD — depends on LCS integration scope

**⚠ DOUBLE-BLOCKED**: Depends on T11 (itself a blocker with sync/async unsolved) AND on LCS integration (external team, unknown scope). Don't plan until T11 is complete and LCS integration surface is documented.

### T13: Alert trigger (R15) [Phase 3b]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No Alertmanager webhook → workflow trigger.

**What to build**: Webhook endpoint that accepts Alertmanager payloads and starts workflows.

**Effort**: 1 week

### T14: Schedule trigger (R15) [Phase 3b] — DONE

**Status**: Done
**ARCHITECTURE.md ref**: Requirements table R15 — TODO

**Problem**: No cron/scheduled workflow execution.

**What to build**: Expose Temporal's native cron schedule via API.

**What was built**:
- `schedule_trigger.py`: Pydantic models (`ScheduleSpec`, `ScheduleInput`, `ScheduleInfo`)
  with cron expression validation (5-field standard + Temporal shorthands)
- CRUD endpoints via `build_schedule_router()` on separate `APIRouter(prefix="/v1/schedules")`:
  POST (create), GET list, GET by id, DELETE, POST pause, POST resume
- Leverages Temporal's native Schedule API (not cron_schedule on start_workflow)
- Schedule-specific `WorkflowAction` enum values: `SCHEDULE_CREATE`, `SCHEDULE_VIEW`,
  `SCHEDULE_DELETE`
- Audit event types: `schedule_created`, `schedule_deleted`, `schedule_triggered`
- `ls_schedule_triggers_total` Prometheus counter with workflow_name/status labels
- Opt-in via `SCHEDULE_TRIGGER_ENABLED=true` env var
- CallerIdentity with `auth_mode="scheduler"` for schedule-triggered workflows

**Effort**: 2-3 days

---

## Escalation & Handoff (R17)

### T15: Interactive CLI handoff (R5, R17) [Phase 4]

**Status**: Open
**ARCHITECTURE.md ref**: Requirements table R17 — TODO; Design Principle R5 — TODO

**Problem**: Escalation packages context but doesn't hand off to an interactive CLI session.

**What to build**: Start with "generate a launch command with pre-loaded context" — a serialized context package that a human can load into Claude Code or Goose. NOT launching an interactive session programmatically (that's weeks with security implications).

**Effort**: 2-3 days for launch command generation; 3-4 weeks for interactive session (deferred)

**Scope recommendation**: Phase 1 of T15 = context serialization + launch command. Phase 2 = interactive session lifecycle. Ship the useful part first.

### T16: Conversational approval [Phase 4]

**Status**: Open (from BACKLOG.md)

**Problem**: Approval gates pause workflows but the only channels are Slack/webhook. No in-conversation approval.

**What to build**: When a workflow hits an approval gate, the LLM surfaces it to the user in natural language; user approves/rejects in the conversation flow.

**Effort**: TBD — depends on chatbot integration

---

## Agent Progress Streaming

### T36: Stream agent work-in-progress to callers [Phase 4]

**Status**: Open
**ARCHITECTURE.md ref**: Observability; Sandbox Runtime

**Problem**: The workflow activity makes a synchronous HTTP call to the sandbox and waits for the final result. The sandbox streams internally from the LLM (the OpenAI agents SDK supports it) but collapses everything into a single response. Callers see only workflow-level events (step started/completed) via SSE — no token-by-token output, tool calls, or intermediate results from the agent.

**⚠ REQUIRES SANDBOX CHANGES**: The sandbox (`lightspeed-agentic-sandbox`) currently has no mechanism to push progress events externally. It has three internal observability layers — but none stream to the caller:

1. **OTel spans** (`tracing.py`): Exported via gRPC OTLP to a collector (if configured). Creates `agent.run` and `tool.{name}` spans. But spans are batch-exported (~5s delay), the parent span doesn't appear until completion, and span attributes don't carry tool input/output content. OTel spans are for post-hoc tracing, not live progress.

2. **Event logging** (`logging.py`): `EventLogger` writes thinking, tool calls, and results to Python `logging` (stdout). Visible via `kubectl logs` only — not streamed anywhere.

3. **Audit events** (`audit.py`): `AuditLogger` writes structured JSON lines to stdout with trace_id, tool names, and content. Also creates OTel spans per tool call. But like event logging, this only goes to stdout.

The LLM provider SDKs stream events internally (`async for event in result` in `query.py`), and the sandbox processes each event through `EventLogger` and `AuditLogger`. But all output stays inside the sandbox pod — nothing flows back to the orchestrator or any external consumer.

**What the sandbox needs** (upstream work, requires coordination with lightspeed-agentic-sandbox team):
- Read `progressUrl` and `progressToken` from the `/v1/agent/run` request body
- During the `async for event in result` loop, POST progress events to `progressUrl` with bearer auth
- Event types: `llm_token` (streaming text), `tool_call` (name + input), `tool_result` (name + output)
- Failure handling: if the callback is unreachable, log and continue (don't break the primary agent execution)
- This is the longest lead-time item — cross-team contract change that needs early alignment

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

### T23: Rate limiting [Phase 3b] -- DONE

**Status**: Done

**What to build**: Per-user request-level rate limiting. Existing spawner/worker concurrency caps handle pod storms; this adds API-level throttling for multi-tenant.

**Effort**: 2-3 days

### T24: Pod disruption budgets [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: PDB template in Helm chart: `minAvailable: 1` when replicas > 1.

**Effort**: Half day

---

## Workflow Features (from BACKLOG.md)

### T25: Nested workflows [Phase 5]

**Status**: Open

Workflow-to-workflow composition (recursive execution).

**⚠ HIDDEN COMPLEXITY**: Temporal supports child workflows, but Cloud Agents routes everything through `run_sandbox_step`. Nested workflow either bypasses sandbox (new step type needed) or creates circular dependency (sandbox calls back to runner API). Resource exhaustion risk: 3-step nesting 3-step = 6+ pods, `MAX_SPAWNED_PODS` is global with no per-workflow budget. Approval propagation undefined: if nested workflow hits approval gate, does parent block?

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

### T33: SBOM / SLSA provenance [Phase 5]

**Status**: Open

Image signing attestation and software bill of materials.

### T44: Podman Enterprise mode support [Phase 5]

**Status**: Open

**Problem**: PodmanSpawner connects to the local Podman socket only. In Ansible Enterprise mode (multi-VM), containers are distributed across VMs — the spawner needs to reach remote Podman sockets.

**What to build**:
1. **Per-container resource limits** — pass `--cpus` and `--memory` to `containers.run()` from SpawnConfig values. In Growth mode (single VM) the VM caps everything, but Enterprise mode has multiple workloads per VM.
2. **Remote Podman socket** — support `PODMAN_SOCKET_URL` env var for remote Podman API connections. The spawner currently uses the default local socket.
3. **Cross-VM networking** — sandbox containers on different VMs need to reach each other and the workflow runner. May require Podman network configuration per VM.

**Context**: Ansible containerized deployment has two modes:
- **Growth** (single VM): current Podman spawner works — VM is the boundary
- **Enterprise** (multi-VM): needs remote spawning, resource limits, cross-VM networking

**Effort**: 2-3 weeks

**⚠ RISKS**:
- Remote Podman API may have different auth/TLS requirements per deployment
- Cross-VM networking depends on the Ansible installer's network topology
- Resource limit enforcement on Podman differs from K8s (no cgroups v2 on some distros)

### T34: Multi-replica E2E testing [Phase 4]

**Status**: Open

2-replica workflow runner deployment with Temporal. Test: start workflow on replica A, kill replica A, verify Temporal re-dispatches activities to replica B and workflow completes.

### T35: CRD-based K8s operator [Phase 5]

**Status**: Open (from kubeclaw comparison)

**Effort**: 6-8 weeks (revised up — "thin bridge" is an iceberg)

**⚠ MASSIVELY UNDERESTIMATED**: The existing agentic operator has 15+ type files, reconcilers, finalizers, owner references, CEL validation, and e2e tests. A "bridge" still needs CRD types (Go structs, deepcopy, generated manifests), reconciler, status sync (Temporal state → CRD status), cleanup via finalizers, and RBAC mapping. Consider whether the existing `lightspeed-agentic-operator` could be refactored to call the Cloud Agents API instead of reimplementing.

---

## Security & Governance Hardening

### T37: Secret redaction in logs and error responses [Phase 3a] -- DONE

**Status**: Done (PR #7)

**Problem**: `credentials_secret` value is injected as a plain env var on sandbox pods. If a sandbox error response includes environment details or the activity logs the full env dict, secrets leak into logs or API responses. Audit events include `secret_name` but the activity doesn't redact credential values from error paths.

**What to build**:
- Redact known secret env var values from error responses before returning to callers
- Redact secret values from log messages in the activity (never log `env_vars` dict raw)
- Track which env vars contain secrets through spawner into activity error handler
- Add a test that triggers an error path and asserts no secret values appear in the response or logs

**Effort**: 2-3 days (revised up from 1 day — secret tracking through spawner is non-trivial)

### T38: Request body size limits [Phase 3a] -- DONE

**Status**: Done (PR #22)

**Problem**: `POST /v1/workflows/run` accepts arbitrarily large definition/prompt payloads. A malicious or misconfigured client could submit a multi-MB definition to exhaust memory or Temporal payload limits.

**What was built**:
- `ContentSizeLimitMiddleware` ASGI middleware in `src/cloud_agents/workflow/middleware.py`
- Checks Content-Length header (fast path) and counts bytes from receive() (chunked encoding)
- Returns 413 with descriptive error when exceeded
- Wired into `temporal_entrypoint.py` after CORS middleware
- Configurable via `MAX_REQUEST_BODY_BYTES` env var (default 1 MB)
- 7 unit tests covering oversized Content-Length, oversized chunked body, normal payloads, GET bypass, exact limit boundary, error message content, and non-HTTP scope passthrough

**Effort**: Half day

### T39: Sandbox network egress enforcement by default [Phase 3a] -- DONE

**Status**: Done (PR #26)

**Problem**: Sandbox containers can make outbound requests to any endpoint, not just the LLM provider. NetworkPolicy exists in Helm but is opt-in (`networkPolicy.egress.enabled: false`). A compromised or malicious agent could exfiltrate data to arbitrary hosts.

**What was built**:
- Helm default flipped to `networkPolicy.egress.enabled: true`
- Kind `deploy/kind/network-policy.yaml` extended with egress rules for workflow-runner and sandbox pods
- `make kind-up` applies network-policy.yaml
- DEPLOYMENT.md documents egress configuration for Helm, Kind, and Podman (iptables/nftables examples)
- ARCHITECTURE.md security section updated with egress enforcement

**Effort**: Half day (Helm change + docs)

### T40: Prompt injection guardrails [Phase 5]

**Status**: Open

**Problem**: Workflow definitions are submitted as arbitrary dicts. Pydantic validates schema but doesn't restrict prompt/instruction content. A malicious prompt could instruct the LLM to exfiltrate data, ignore safety guidelines, or produce harmful output. This is especially relevant when non-admin users can trigger workflows (post-RBAC).

**What to build**:
- Design decision needed: input-side filtering (reject suspicious prompts) vs output-side filtering (scan agent output) vs both
- Consider integration with existing guardrail frameworks (pydantic-ai-shields, llm-guard)
- At minimum: log a warning when prompts contain known injection patterns (e.g., "ignore previous instructions", "system prompt override")

**Effort**: TBD — needs design discussion. Logging-only detection is 1-2 days; full guardrail integration is 1-2 weeks.

### T41: Audit log integrity [Phase 5]

**Status**: Open

**Problem**: Audit events go to stdout/stderr via structured logging. No signed audit trail, no tamper-evident log chain, no guaranteed delivery. An operator with log access could modify audit records.

**What to build**:
- Append audit events to a dedicated audit log file (separate from application logs)
- Add HMAC signatures or hash chain for tamper detection
- Optionally: forward audit events to an external audit service (webhook)

**Effort**: 1-2 days for file-based audit log; 1 week for signed/chained logs

### T42: Token rotation and expiry for bearer auth [Phase 3a] -- DONE

**Status**: Done (PR #25)

**Problem**: Bearer tokens are static (`AGENT_API_TOKEN` env var). No rotation mechanism, no expiry. A leaked token grants permanent access until the env var is manually changed and the runner restarted.

**What was built**:
- Multi-token support via `AGENT_API_TOKENS` env var (comma-separated), backward compatible with `AGENT_API_TOKEN`
- Optional per-token expiry via `token:unix_timestamp` suffix format
- Rejected token logging with prefix (first 4 chars) -- never logs full token
- `auth_rejected` audit event emitted on token rejection (invalid or expired)
- `emit_audit()` workflow_id made optional for pre-workflow events
- `create_bearer_auth_dependency()` factory returns a proper FastAPI dependency (closure) instead of returning the middleware class
- 43 unit tests covering multi-token, backward compat, rejection logging, audit events, expiry, and dependency wiring

**Effort**: 1 day

### T43: Workflow definition content policy [Phase 3a]

**Status**: Done (PR #20)

**Problem**: RBAC controls who can submit definitions, but not what definitions contain. A user with `manage_defs` permission could submit a definition with instructions that bypass organizational policies (e.g., "ignore safety guidelines", "access all namespaces").

**What to build**:
- Definition content policy: configurable rules that validate definition content at submission time
- Examples: max prompt length, blocked instruction patterns, required output_schema fields, namespace restrictions
- Policy violations return 422 with details

**Effort**: 1-2 days for basic content rules; 1 week for configurable policy engine

### T49: Validate output_schema before submission [Phase 3b]

**Status**: Done (PR #5)

**Problem**: Users can submit workflow definitions with invalid `output_schema` (e.g. `type: array` without `items`). The framework passes the schema through to the LLM provider, which rejects it at runtime with a cryptic 400 error (e.g. OpenAI: "array schema missing items"). The user sees `agent returned success=false` with no indication that the schema was invalid.

**What to build**:
- Validate `output_schema` in `temporal_validation.py` at definition submission time
- Check JSON Schema validity: arrays must have `items`, objects should have `properties`
- Provider-specific rules: OpenAI structured output requires `additionalProperties: false` on objects (warn if missing)
- Return 422 with clear error message: "output_schema for step 'X': array type requires 'items' definition"

**Effort**: 1 day

### T50: Per-step MCP server config [Phase 3b]

**Status**: Done (commit 507b29e)

**Problem**: `mcp_servers` is set at the workflow run level, so every sandbox in the workflow gets `LIGHTSPEED_MCP_SERVERS` injected. Steps that don't need MCP tools still connect to MCP servers on startup, wasting resources and causing issues when MCP servers can't handle concurrent SSE sessions (e.g. supergateway crashes on second connection while first sandbox is still alive with SKIP_SANDBOX_DESTROY).

**What to build**:
- Allow `mcp_servers` in the step definition (per-step override), not just the run request
- Activity code: if step has `mcp_servers`, use that; otherwise fall back to workflow-level config; if neither, don't inject `LIGHTSPEED_MCP_SERVERS`
- This also enables different steps to use different MCP servers (e.g. step 1 uses filesystem, step 2 uses Jira)

**Effort**: 1 day

### T48: Sandbox per-spawn bearer token auth [Phase 3a]

**Status**: Done (PR #19)

**Problem**: Inter-pod traffic between the workflow runner and sandbox containers had no authentication. Anyone who could reach the sandbox network could call `POST /v1/agent/run` without credentials.

**What was built**:
- Per-spawn bearer token via `secrets.token_urlsafe(32)`, injected as `SANDBOX_AUTH_TOKEN` env var
- `Authorization: Bearer {token}` header sent in httpx POST to `/v1/agent/run`
- Gated by `SANDBOX_AUTH_ENABLED` env var (disabled by default for backward compat)
- Health endpoint stays unauthenticated (K8s probes need it)
- 12 unit tests

**Remaining**: TLS encryption moved to T51.

### T51: App-level TLS for runner-to-sandbox encryption [Phase 4] -- DONE

**Status**: Done ([issue #21](https://github.com/jameswnl/lightspeed-cloud-agents/issues/21))

**Problem**: T48 added authentication but traffic is still unencrypted HTTP. Prod sec requires encryption for inter-pod communication. Deployments with a service mesh (Istio) get mTLS transparently, but Podman and non-mesh K8s deployments need app-level TLS.

**What to build**:
- Ephemeral cert generation utility (`tls.py`): CA + server cert per spawn, valid 10 minutes
- K8s: cert Secret creation + volume mount + cleanup in `_do_destroy`
- Podman: temp dir with cert files + bind mount
- `SANDBOX_TLS_MODE`: `app` (app-level TLS), `mesh` (skip, mesh handles it), disabled by default
- `cryptography>=44.0` added to pyproject.toml optional deps

**Effort**: 3 days

**⚠ RISKS**:
- App-level TLS adds ~100ms per spawn for cert generation
- Podman has no mesh equivalent — app-level TLS is the only option

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
