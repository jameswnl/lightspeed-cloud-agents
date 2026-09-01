# Implementation Plan (Archived 2026-09)

**This document is frozen and no longer maintained.** New work is tracked directly as GitHub issues (matching how #236, #238, #239, and #240 were already handled) rather than as entries in this file. Of the 61 distinct T-numbered items below, 44 were already Done/Removed/Obsolete/Resolved at archival time (including a few whose resolution was recorded only as a "— Done"/"— DONE" title suffix rather than a `**Status**` line — double-check both when reading this file); the remaining 17 were resolved as follows:

- **14 unscheduled, no-owner ideas** consolidated into one roundup issue: [#242](https://github.com/jameswnl/lightspeed-cloud-agents/issues/242) (T8, T9, T11, T12, T16, T25, T26, T27, T30, T31, T32, T33, T40, T41).
- **T36** (progress streaming), which carried a full design draft and two rounds of reviewer feedback, got its own issue to preserve that context: [#241](https://github.com/jameswnl/lightspeed-cloud-agents/issues/241).
- **T15** was actually already fully resolved (its one open follow-up shipped via T56/PR #69/issue #66) — corrected in place below rather than re-filed.
- **T29** and **T35** were marked Obsolete in place (both predate the OpenShell-only consolidation in issue #198) rather than carried forward.
- **T55** was already tracked as [issue #65](https://github.com/jameswnl/lightspeed-cloud-agents/issues/65) — left as-is.
- **T53** was already "Substantially done" — left as-is.

This file is preserved for historical context (effort estimates, design rationale, "Done" narratives with test coverage detail) — see `docs/archived/prod/implementation-plan.md` and `docs/gaps/phase-1-records/`/`phase-2-records/` for the same archival pattern applied to earlier phases of this same document.

---

Single source of truth for all planned work. ARCHITECTURE.md TODO tags link here.

Items are organized by area. Each has a status: **Open**, **Decided**, **Closed**, or **Done**.

## Priority Phases

| Phase | Focus | Tasks |
|-------|-------|-------|
| **Phase 1** | High value, enables other work | T1 ✓, T3 ✓, T22 ✓ |
| **Phase 2** | Production hardening | T7 ✓, T17 ✓, T19 ✓, T21 ✓, T24 ✓ |
| **Phase 3a** | Security quick wins | T37 ✓, T38 ✓, T39 ✓, T42 ✓, T43 ✓, T48 ✓ |
| **Phase 3b** | Triggers + hardening | T2 ✓, T13 ✓, T14 ✓, T23 ✓, T49 ✓, T50 ✓ |
| **Phase 4** | Strategic (needs design first) | T8, T11, T15 ✓*, T34 ✓, T36, T51 ✓, T52 ✓, T53, T54 ✓, T55 ✓, T57 ✓ |
| **Phase 5** | Backlog | T5 ✓, T9, T12, T16, T18 ✓, T20 ✓, T25-T27, T29-T33, T35, T40, T41, T56 |

*T15 Phase 1+2 done, follow-ups T55 (timeout) and T56 (bi-directional) pending

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

**Status**: Done ([issue #44](https://github.com/jameswnl/lightspeed-cloud-agents/issues/44))
**ARCHITECTURE.md ref**: Security — per-step tool filtering (R12 Partial)

**Problem**: `allowed_tools` / `denied_tools` from `WorkflowStepSpec.permissions` were never passed to the sandbox. Per-step tool scoping was defined in the model but not enforced in the workflow path.

**What was built**:
1. In `temporal_activities.py`, `allowed_tools` and `denied_tools` from `step.permissions` are extracted and included as `allowedTools`/`deniedTools` in the `request_body` sent to the sandbox.
2. `PermissionScope.effective_tools()` method computes filtered tool sets from allowed/denied lists.
3. K8s MCP kubectl server (`deploy/mcp-kubectl/Containerfile`) using `@anthropic-ai/mcp-server-kubernetes` + `supergateway` on port 8082.
4. K8s manifests (`examples/kind-mcp-kubectl.yaml`) with ServiceAccount, scoped RBAC (read: pods, pods/log, events, deployments, services, nodes, replicasets; limited write: deployments patch, deployments/scale patch), Deployment, and Service.
5. Network policy updated to allow sandbox egress to mcp-kubectl (port 8082).
6. Real-cluster workflow definition (`examples/workflow-definitions/k8s-realcluster-workflow.yaml`) with per-step `permissions.allowed_tools` for tool filtering.
7. Makefile: `build-mcp-kubectl` target, `build-demo` and `kind-up` wiring.
8. 30+ unit tests validating Containerfile, K8s manifests, RBAC, network policy, workflow definition, tool filtering logic, and Makefile targets.

**Remaining**: Sandbox-side enforcement is pending (separate repo: lightspeed-agentic-sandbox). The runner forwards the fields; the sandbox must consume them.

**Effort**: 1 day (runner + infrastructure)

### T2: Explicit sandbox termination on timeout/cancellation [Phase 3b] — DONE

**Status**: Done ([issue #13](https://github.com/jameswnl/lightspeed-cloud-agents/issues/13))
**ARCHITECTURE.md ref**: Temporal Server — explicit sandbox termination on timeout

**Problem**: When a Temporal activity times out, cleanup is best-effort in `finally`. No heartbeat, no explicit kill signal if worker crashes mid-timeout.

**What was built**:
- `_heartbeat_loop()` async helper in `temporal_activities.py` — heartbeats every 30s during sandbox HTTP call
- `asyncio.CancelledError` handler sets `was_cancelled` flag, ensures `destroy()` runs
- `heartbeat_timeout=timedelta(seconds=180)` on activity execution (accommodates cold pod starts)
- `ls_sandbox_timeout_total` Prometheus counter with `step_name` and `reason` labels
- `sandbox_timeout` audit event type
- 9 unit tests (heartbeat, cancellation handling, timeout metrics)

**Effort**: 1 day

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

### T5: Document runtime input completeness [Phase 4] — DONE

**Status**: Done ([issue #35](https://github.com/jameswnl/lightspeed-cloud-agents/issues/35))
**ARCHITECTURE.md ref**: Sandbox Runtime config table

**What was built**:
- Added `LIGHTSPEED_MODEL_PROVIDER`, `LIGHTSPEED_SERVICE_ACCOUNT`, `SANDBOX_TLS_CERT_PATH`/`SANDBOX_TLS_KEY_PATH` to ARCHITECTURE.md config table
- Doc-code sync test (`test_doc_env_sync.py`) that parses the config table, greps source for all env vars set on sandbox containers, and asserts completeness

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

### T13: Alert trigger (R15) [Phase 3b] — DONE

**Status**: Done ([issue #14](https://github.com/jameswnl/lightspeed-cloud-agents/issues/14))
**ARCHITECTURE.md ref**: Requirements table R15

**Problem**: No Alertmanager webhook → workflow trigger.

**What was built**:
- `alert_trigger.py`: Pydantic models (`AlertmanagerAlert`, `AlertmanagerPayload`, `AlertTriggerConfig`), alert-to-workflow mapping, dedup tracker, `build_alert_router()` at `/v1/webhooks/alertmanager`
- RBAC enforcement via authorizer param, content policy validation on stored definitions
- Prompt sanitization: alert labels/annotations truncated to 2000 chars
- Configurable namespace via `ALERT_TRIGGER_NAMESPACE` env var
- `ls_alert_triggers_total` Prometheus counter, `alert_triggered`/`alert_validation_failed` audit events
- 37 unit tests

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

**Status**: Done -- Phase 1 + Phase 2 below, plus the one deferred follow-up (bi-directional communication), fully landed via [T56](#t56-bi-directional-cli-session-communication-phase-5) (Done, PR #69, issue #66). Corrected during the 2026-09 archival pass -- previously read "Partial (follow-ups pending)" even though the only follow-up was already resolved.
**ARCHITECTURE.md ref**: Requirements table R17 — TODO; Design Principle R5 — TODO

**What was built** (Phase 1 — context serialization + launch command):
- Enriched `EscalationPackage` with `definition`, `input_prompt`, `events`, `provider_name`, `workflow_id`
- `serialize_handoff_context()` produces structured markdown for Claude Code consumption
- `CLIHandoffPackager` writes context files and logs launch commands
- `cli_handoff` and `jira` escalation types wired in `temporal_activities.py`
- `GET /v1/workflows/{id}/handoff` endpoint with RBAC (`WorkflowAction.VIEW`)
- `get_workflow_context` Temporal query for stashed definition/provider context
- `workflow.escalated` SSE event emitted after escalation activity completes
- Enriched context passed through workflow -> escalation activity pipeline

**Phase 2** (Done — [issue #59](https://github.com/jameswnl/lightspeed-cloud-agents/issues/59)):
- `CLISessionLauncher` wraps `spawner.spawn()` with CLI entrypoint (credential-scoped, container-isolated)
- `CLIHandoffPackager` auto-launches sessions when `CLI_HANDOFF_AUTO_LAUNCH=true`
- `GET/DELETE /v1/cli-sessions` API endpoints with RBAC (VIEW/CANCEL)
- `cli_session_launched`, `cli_session_terminated`, `cli_session_failed` audit events
- Bi-directional communication deferred to separate issue (Task 5 split out)
### T55: Session timeout enforcement [Phase 4]

**Status**: Open ([issue #65](https://github.com/jameswnl/lightspeed-cloud-agents/issues/65))

**Problem**: `CLISessionLauncher.max_session_seconds` is stored but never enforced. Sessions run indefinitely.

**What to build**: Background asyncio task in `CLISessionLauncher` that periodically checks session age, auto-terminates expired sessions via `spawner.destroy()`, emits `cli_session_terminated` audit event with reason `timeout`.

**Effort**: 1 day

### T56: Bi-directional CLI session communication [Phase 5]

**Status**: Done (PR #69) ([issue #66](https://github.com/jameswnl/lightspeed-cloud-agents/issues/66))

**What was built**:
- `write_file(agent_name, path, content)` on `AgentSpawner` ABC with `_do_write_file()` abstract + 3 implementations (K8s: kubectl exec stdin, Podman: podman exec stdin, OpenShell: base64 via exec_stream)
- `SessionOutputEvent` model + `monitor_output()` async generator with byte offset tracking (polls `read_file()` every 2s)
- `send_message(session_id, message)` writes JSONL to `/var/run/cli-session/messages.jsonl` via `spawner.write_file()`
- `session_result` Temporal signal + `get_session_results` query on `AgentWorkflow`
- `POST /v1/cli-sessions/{id}/messages` endpoint with RBAC (`SESSION_MESSAGE`)
- `GET /v1/cli-sessions/{id}/output` SSE streaming endpoint with RBAC (`VIEW`)
- `SESSION_MESSAGE` added to `WorkflowAction` enum
- `cli_session_message_sent` audit event type

**Effort**: 1 day

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

### T18: Operational runbooks [Phase 4] — DONE

**Status**: Done ([issue #36](https://github.com/jameswnl/lightspeed-cloud-agents/issues/36))

**What was built**:
- `docs/operations/runbook.md` covering 9 failure scenarios: health checks, orphaned sandboxes, stuck workflows, LLM provider errors, sandbox spawn failures, rate limiting, TLS errors, alert/schedule triggers, general diagnostics
- Every metric, endpoint, env var, Makefile target, and source file referenced in the runbook is validated by 12 unit tests to prevent drift
- DEPLOYMENT.md updated with link to the runbook
- Alert reference table linking all PrometheusRule alerts to runbook sections

**Effort**: 1 day

### T19: Circuit breaker for LLM provider [Phase 2] — DONE

**Status**: Open (from productization-roadmap.md P1)

**What to build**: Track recent failures per provider. After N consecutive failures, fail fast instead of spawning sandbox pods that will time out.

**Effort**: 1-2 days

### T20: Load and stress testing [Phase 4] -- DONE

**Status**: Done (issue #37) (from productization-roadmap.md P1)

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

**Status**: Done ([issue #16](https://github.com/jameswnl/lightspeed-cloud-agents/issues/16))

**Problem**: No per-user API-level throttling for multi-tenant deployments.

**What was built**:
- `rate_limiter.py`: `TokenBucket` class (per-key buckets, stale cleanup, rate=0 bypass) and `RateLimitMiddleware` ASGI middleware
- Key derivation: `sha256(bearer_token)[:16]` for privacy, client IP fallback
- 429 response with `Retry-After` header (`max(1, math.ceil(1/rate))`)
- Health/metrics endpoints exempt
- `ls_rate_limit_rejections_total` Prometheus counter, `rate_limit_exceeded` audit events (throttled: 1 per key per 60s)
- Env vars: `RATE_LIMIT_ENABLED`, `RATE_LIMIT_RATE`, `RATE_LIMIT_BURST`
- 25 unit tests

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

**Status**: Obsolete -- predates the OpenShell-only consolidation ([issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198)). `KubernetesSpawner` was deleted entirely; there is no client-side K8s pod spec for cloud-agents to add image volumes to anymore. Skills are baked into the sandbox image at build time instead ([issue #202](https://github.com/jameswnl/lightspeed-cloud-agents/issues/202)). Not carried forward into a GitHub issue.

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

**Status**: Obsolete -- `PodmanSpawner` was deleted entirely in [issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198) (OpenShellSpawner is now the sole ephemeral spawner, not deprecated-with-fallback). Remote/multi-VM sandbox placement for Enterprise mode would need to be solved via the OpenShell gateway's own compute driver instead, not a client-side spawner change. Left here as historical context for that future design conversation, not as an active task.

**Problem** (historical): PodmanSpawner connected to the local Podman socket only. In Ansible Enterprise mode (multi-VM), containers are distributed across VMs — the spawner would have needed to reach remote Podman sockets.

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

### T34: Multi-replica E2E testing [Phase 4] -- DONE

**Status**: Done ([issue #38](https://github.com/jameswnl/lightspeed-cloud-agents/issues/38))

**Problem**: No tests verifying Temporal's crash recovery with multiple workflow runner replicas. Need to prove the stateless runner claim (R7) holds under replica failure.

**What was built**:
- `deploy/kind/workflow-runner-2-replicas.yaml`: 2-replica Kind overlay (identical to base except `replicas: 2`)
- `tests/e2e/features/multi_replica.feature`: BDD scenarios for crash recovery, orphan cleanup, concurrent workflows
- `tests/e2e/features/steps/multi_replica_steps.py`: pytest-bdd step definitions with kubectl helpers
- `tests/e2e/features/steps/test_multi_replica_bdd.py`: pytest-bdd runner (auto-skips when no Kind cluster)
- `tests/unit/test_multi_replica_overlay.py`: 13 unit tests validating overlay YAML (drift guard, security context, task queue)
- `tests/unit/test_multi_replica_helpers.py`: 10 unit tests for kubectl helper functions
- `tests/e2e/test_list.txt`: complete E2E scenario inventory
- `Makefile`: `test-multi-replica` target (deploys overlay, waits for readiness, runs tests)
- `pyproject.toml`: added `pytest-bdd>=8.0` to dev dependencies

**Effort**: 1 day

### T35: CRD-based K8s operator [Phase 5]

**Status**: Obsolete -- predates the OpenShell-only consolidation ([issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198)). A CRD/operator bridge onto a client-side K8s spawner no longer fits the architecture -- K8s-specific compute is now solely OpenShell gateway's own concern, not something cloud-agents implements. Not carried forward into a GitHub issue.

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
- `ContentSizeLimitMiddleware` ASGI middleware in `src/cloud_agents/runtime/middleware.py`
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

### T52: E2E test coverage gaps ([issue #41](https://github.com/jameswnl/lightspeed-cloud-agents/issues/41)) — DONE

**Status**: Done (PR #TBD)

**Problem**: All closed features had strong unit test coverage but four areas lacked E2E tests exercising real infrastructure: sandbox auth, alert/schedule triggers, network egress, and full-stack workflow lifecycle.

**What was built**:
1. **Sandbox auth wiring** — `temporal_activities.py` now injects `AGENT_API_TOKEN` on sandbox containers and sends `Authorization: Bearer <token>` on httpx POST when `SANDBOX_AUTH_ENABLED=true`. Uses `get_runner_auth_token()` from `runtime/auth.py`. 7 unit tests + E2E test (`tests/e2e/test_sandbox_auth.py`).
2. **Alert/schedule trigger E2E** — `tests/e2e/test_triggers.py`: full alert webhook flow (register def, POST webhook, verify workflow starts), dedup, missing-definition error. Schedule CRUD lifecycle (create, get, delete, verify gone), duplicate-ID rejection, nonexistent-workflow rejection. Runs against real Temporal in CI.
3. **Network egress E2E** — `tests/e2e/test_egress.py`: validates sandbox pods cannot reach external HTTP hosts, can resolve DNS, can reach in-cluster services. Requires Kind + Calico (local only, skips in CI). Created `deploy/kind/kind-config-calico.yaml`.
4. **Full-stack workflow E2E** — `tests/e2e/test_full_stack.py`: real LLM output validation, sandbox cleanup verification. Requires LLM API key (local only, skips in CI).
5. **CI updates** — `e2e_tests.yaml` now runs trigger E2E tests alongside existing workflow tests.
6. **ARCHITECTURE.md** — documented `AGENT_API_TOKEN` env var in config table.

**Effort**: 1 day

### T53: OpenShell Gateway spawner [Phase 4]

**Status**: Substantially done -- consolidation onto OpenShellSpawner as the sole spawner completed via [issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198) (spike: [issue #50](https://github.com/jameswnl/lightspeed-cloud-agents/issues/50))
**ARCHITECTURE.md ref**: Spawner — unified backend

**Problem** (historical, resolved by #198): We maintained two spawner implementations (KubernetesSpawner and PodmanSpawner) with duplicated logic. Sandbox isolation was limited to container securityContext with no Landlock, seccomp, L7 network policy, or SSRF protection.

**Spike findings**: OpenShellSpawner prototype implements AgentSpawner ABC via OpenShell gRPC API. Single `_do_spawn` replaces both K8s and Podman paths. Gateway handles sandbox lifecycle, network isolation, and credential management. Service exposure via `ExposeService` RPC preserves the `POST /v1/agent/run` contract. Full findings: `docs/spikes/openshell-spawner-spike.md`.

**What to build** (if spike -> go):
1. Production-harden the `OpenShellSpawner` prototype — SDK migrated (PRs #97–#100), parallel-safe readiness, auto-cleanup on failure, non-advisory Landlock baseline filesystem policy so sandboxes can read their own image contents ([issue #189](https://github.com/jameswnl/lightspeed-cloud-agents/issues/189)) ✅, `PYTHONPATH` propagated to the exec'd server process despite the gateway supervisor's `env_clear()` ([issue #192](https://github.com/jameswnl/lightspeed-cloud-agents/issues/192)) ✅, query-time HTTP client trusts the spawner's own gateway TLS CA in both the local and Temporal executors ([issue #194](https://github.com/jameswnl/lightspeed-cloud-agents/issues/194)) ✅, local (non-Temporal) executor now interpolates `{{ steps.X.output.Y }}` in agent step `prompt`/`instructions` (previously only the Temporal executor did this — full end-to-end verification on real OCP surfaced a "remediate" step sending its literal, unsubstituted template to the LLM) ✅
2. Skills redesigned end-to-end per [issue #202](https://github.com/jameswnl/lightspeed-cloud-agents/issues/202) ✅ (in review, [PR #206](https://github.com/jameswnl/lightspeed-cloud-agents/pull/206)): the old runtime `skills_image`/`skills_paths` mount-and-extract mechanism (native podman `driver_config` mount, or crane-extraction + tar-upload fallback on other drivers) is removed entirely from `OpenShellSpawner`, along with the compute-driver auto-detection ([PR #200](https://github.com/jameswnl/lightspeed-cloud-agents/pull/200)) that existed only to pick between those two paths. All available skills are now baked into the sandbox image at build time under `/skills/<name>`, read-only ([lightspeed-agentic-sandbox PR #9](https://github.com/jameswnl/lightspeed-agentic-sandbox/pull/9), merged), and a new per-step `allowed_skills: list[str]` field (`WorkflowStepSpec`, shared across the `AgentSpawner` ABC, validated at `/run` submission time in `validate_definition()`) drives enforcement in `OpenShellSpawner`'s non-advisory policy — at the time, `KubernetesSpawner`/`PodmanSpawner` accepted the field but logged a warning and didn't enforce it (no Landlock equivalent); both classes were deleted entirely in [issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198), so this is no longer a gap.

   Enforcement is two-part because a Landlock read-only grant on `/skills/<name>` alone isn't sufficient: agent providers discover skills by *listing* `LIGHTSPEED_SKILLS_DIR`, and Landlock's allow-list model can't grant partial listing of `/skills` without granting full listing (defeating per-name scoping) — a design gap a human reviewer caught on the first version of this PR. The fix: (1) the Landlock grant on `/skills/<name>` remains as the real enforcement boundary, and (2) before starting the agent server, `OpenShellSpawner` execs `lightspeed-agentic-sandbox`'s baked-in `/usr/local/bin/materialize-skills.sh` with the `allowed_skills` names as argv, copying just those names into `/app/skills` (now `LIGHTSPEED_SKILLS_DIR`) — a plain, freshly-listable directory that providers can enumerate normally. The copy itself still goes through the Landlock grant from (1).

   This makes [issue #201](https://github.com/jameswnl/lightspeed-cloud-agents/issues/201) (the `/app/skills` UID-ownership mismatch) and [issue #106](https://github.com/jameswnl/lightspeed-cloud-agents/issues/106) (crane not installed in the runner image) moot for OpenShellSpawner's old mechanism, but `/app/skills` is back in play under the new one — the `chmod -R g+w /app/skills` fix from #201's investigation is reused directly in the sandbox image's Containerfile. That POSIX permission alone was not sufficient, though: live verification against a real Kind + OpenShell gateway caught that `_build_baseline_filesystem_policy()` only ever granted `/app` `read_only`, so `materialize-skills.sh` failed with `Permission denied` regardless of the image's own `chmod` — Landlock denies writes independently of POSIX permissions it hasn't been told to allow. Fixed by also granting Landlock `read_write` on `/app/skills` specifically (only when `allowed_skills` is set), re-verified against the same gateway (`tests/e2e/test_guardrails.py::test_allowed_skills_scoping_on_real_gateway`, now a permanent regression test): `allowed_skills=["k8s-diag"]` correctly materializes only that skill into `/app/skills`, `/skills/k8s-diag/SKILL.md` is readable, and `/skills/git-ops/SKILL.md` is still Landlock-denied. Command injection via request-supplied `skills_paths` in all three spawners (`docs/CODE-REVIEW.md` finding #3) was fixed separately with argv-form `cp`/`crane export` (with `--` end-of-options hardening) before this redesign landed; that history is moot now that `KubernetesSpawner`/`PodmanSpawner` (and their `skills_image`/`skills_paths` mechanism) are deleted entirely ([issue #198](https://github.com/jameswnl/lightspeed-cloud-agents/issues/198)).
3. Add credential provider integration (replace K8s Secret env vars)
4. L7 network policy configuration via SandboxPolicy — auto-derived from provider + MCP config via `_build_network_policy()` (PR #102) ✅
5. Integration tests with a running OpenShell gateway — `tests/e2e/test_guardrails.py::TestOpenShellGuardrails` against a real Kind + podman-driver gateway ([issue #189](https://github.com/jameswnl/lightspeed-cloud-agents/issues/189)) ✅
6. Deployment guide: gateway setup for K8s and Podman deployment targets, and the strict pairing between the gateway's own compute driver and its deployment target -- invisible to the client, see `docs/architecture-with-openshell.md` ✅ (issue #198)

**Effort**: 2-3 weeks (production), 1 week (spike done)

**Risks**:
- OpenShell is alpha software — API may change
- Python SDK lacks `ExposeService` wrapper — using standalone gRPC channel (not `client._stub`)
- `SandboxClient.list()` has no label filter — relies on naming convention
- Gateway is a new infrastructure dependency to operate

### T54: Agent step transcript persistence ([issue #57](https://github.com/jameswnl/lightspeed-cloud-agents/issues/57)) -- DONE

**Status**: Done (PR #TBD)

**Problem**: When a workflow step completes (or fails), only the final result is retained in Temporal. The agent's multi-turn loop (tool calls, thinking, errors) is lost when the container is destroyed.

**What was built**:
1. `TranscriptEvent` and `StepTranscript` Pydantic models with smart truncation (keeps tool names/durations, drops large payloads)
2. `read_file()` method on `AgentSpawner` ABC with implementations for OpenShell, K8s, and Podman spawners
3. `_collect_transcript()` helper in activities: reads `/var/log/agent-events.jsonl` after HTTP result, before destroy, with graceful degradation when file doesn't exist
4. `_step_transcripts` dict in `AgentWorkflow` + `@workflow.query get_step_transcripts()` for retrieval
5. `GET /v1/workflows/{id}/steps/{step}/transcript` API endpoint with RBAC (WorkflowAction.VIEW)
6. Transcript data wired into CLI handoff: `step_transcripts` field on `EscalationPackage`, tool call chain rendered in `serialize_handoff_context()` for failed steps, `/handoff` endpoint queries and includes transcripts

**Remaining**: Task 5 (sandbox event file producer) is in a separate repo (lightspeed-agentic-sandbox). Tasks 1-4b produce the consumer side; the producer is tracked in issue #52.

**Effort**: 2 days

### T54-followup: Sandbox JSONL event file producer env var wiring ([issue #61](https://github.com/jameswnl/lightspeed-cloud-agents/issues/61)) -- DONE

**Status**: Done

**Problem**: The sandbox event log producer (activated via `AGENT_EVENT_LOG` env var) was implemented in lightspeed-agentic-sandbox (PR #101) but the cloud-agents workflow runner never set the env var on spawned containers.

**What was built**:
1. Set `AGENT_EVENT_LOG=/var/log/agent-events.jsonl` in sandbox env vars in `temporal_activities.py`, using the existing `_EVENT_LOG_PATH` constant
2. Added `AGENT_EVENT_LOG` to ARCHITECTURE.md sandbox config table
3. Added dual truncation documentation (producer: 2000 chars, consumer: 256 bytes)
4. Unit test verifying the env var is set correctly

**Effort**: 15 minutes

### T57: Full transcript persistence in PostgreSQL ([issue #76](https://github.com/jameswnl/lightspeed-cloud-agents/issues/76)) [Phase 4] -- DONE

**Status**: Done

**Problem**: Step transcripts are truncated aggressively for Temporal workflow query state storage (256 bytes per field, max 50 events). The full JSONL from the sandbox is discarded after truncation. Once the container is destroyed, the full transcript is lost permanently.

**What was built**:
1. `TranscriptStore` class (`src/cloud_agents/storage/transcript_store.py`) using `asyncpg` with async methods: `save`, `get`, `list_steps`, `delete_workflow`, `cleanup_expired`. Schema auto-migrated via `CREATE TABLE IF NOT EXISTS` on `connect()`. `TRANSCRIPT_DB_URL` and `TRANSCRIPT_RETENTION_DAYS` (default 30) env vars.
2. Entrypoint wiring: `TranscriptStore.from_env()` at startup, `connect()` in lifespan (best-effort, non-fatal), `close()` on shutdown, passed to both worker config and router.
3. Activity wiring: full untruncated transcript saved to Postgres after `_collect_transcript()`, before truncation for workflow query state. Save failures are non-fatal (best-effort).
4. Transcript API: `GET /steps/{step}/transcript` reads from Postgres first (full, `truncated: false`), falls back to workflow query state (`truncated: true`). Graceful degradation on Postgres failure.
5. Escalation wiring: `build_escalation_activity` pulls full transcripts from Postgres for `EscalationPackage.step_transcripts`. `/handoff` endpoint also pulls from Postgres when available.
6. Deployment: `TRANSCRIPT_DB_URL` added to docker-compose (defaults to Temporal's Postgres), Helm values (`transcriptStore.dbUrl`, `retentionDays`), and deployment template (conditional env vars).
7. 37 new unit tests covering all components.

**Remaining**: Task 2 (HTTP collection via `GET /v1/agent/events`) is separable and tracked in the issue. Current implementation uses existing `spawner.read_file()` path.

**Effort**: 1 day

### T58: Temporal engine observes per-step spawn mode ([issue #228](https://github.com/jameswnl/lightspeed-cloud-agents/issues/228)) -- Done (PR #229)

**Status**: Done

**Problem**: The Temporal engine ignored `step["spawn"]` entirely -- `_handle_agent_step()` (`temporal/workflow.py`) always scheduled the `run_sandbox_step` activity regardless of `spawn: none`/`local`/`ephemeral`, so a step marked `spawn: none` silently ran as `ephemeral` (spawning a sandbox, or hitting the "no spawner configured" stub) instead of an in-process/subprocess LLM call like the local engine (`LocalWorkflowRunner`) does via `get_step_executor()`.

**What was built**:
1. Dispatch branch added inside `_run_sandbox_step_inner` (`temporal/activities.py`), not in `_handle_agent_step()`'s `execute_activity()` call -- Temporal replay keys on activity type name + schedule order, not activity body, so branching inside the activity implementation keeps in-flight workflow histories replay-safe. The scheduled activity type stays `run_sandbox_step` for every spawn mode.
2. `spawn: none`/`local` now call the same `get_step_executor()` (`DirectExecutor`/`SubprocessExecutor`) the local engine uses, via a new `_run_direct_or_local_step()` helper: builds a `StepInput` matching `graph_translator.py`'s field-for-field construction, wraps the executor call in the existing `_heartbeat_loop()` pattern (required -- `heartbeat_timeout=180s` would otherwise kill a long LLM call), records circuit-breaker success/failure, normalizes the returned transcript events via `normalize_transcript_events()` before returning (required -- `_handle_agent_step()` builds `StepTranscript(**transcript_data)` directly with no normalization, so raw flat `DirectExecutor`-shaped events would fail Pydantic's `Literal` validation), and persists via `TranscriptMiddleware` when a store is configured.
3. New gap found during implementation and fixed as part of this same change: `instructions` was never interpolated on the Temporal side for *any* spawn mode (only `prompt` was) -- the ephemeral path passed it raw as `systemPrompt`. Added `_interpolate_instructions()`, a fail-open helper mirroring `graph_translator.py::_interpolate_step_text()` (deliberately does not handle `{{ input }}` -- that's `_interpolate_prompt`-specific and instructions never supported it on either engine). Wired into **both** the none/local `StepInput.system_prompt` construction and the ephemeral path's `request_body["systemPrompt"]` assignment -- an earlier draft of this fix only wired the former, caught in review since it left ephemeral (the default spawn mode) with the original bug and created a new none/local-vs-ephemeral inconsistency.
4. The `spawner is None` stub (`"executed-{step_name}"`) stays ephemeral-only -- `spawn: none`/`local` always actually call the LLM now, regardless of whether an ephemeral spawner is configured. This is an intentional behavior change from before, where every Temporal agent step was stubbed when no spawner was configured.
5. `mcp_servers` for `none`/`local` passes the full unfiltered workflow-level catalog, matching the local engine's actual current behavior (a step's own `mcp_servers:` field is a no-op for `none`/`local` on both engines today) -- not the ephemeral path's per-step-name filtering.
6. 16 new tests: `tests/unit/workflow/temporal/test_activities.py::TestSpawnModeDispatch` (spawner never called, unknown mode raises, stub stays ephemeral-only, transcript round-trip survives `StepTranscript` validation, `instructions` interpolation + fail-open, heartbeat, full MCP catalog, transcript persistence, circuit breaker success/failure/open-blocks-none-too), `tests/unit/workflow/temporal/test_workflow.py::TestSpawnModeReplayContract` (a real `WorkflowEnvironment` + `Worker` run confirms `spawn: none` still resolves via the `run_sandbox_step` activity registration), `tests/unit/workflow/executor/test_step_dispatch.py::TestSpawnModeEngineParity` (structural: `get_step_executor()` never passes the spawner to `DirectExecutor`/`SubprocessExecutor` regardless of caller).
7. `CLAUDE.md`'s "Active fields: spawn mode" section updated to state parity across both engines and the caveats that now apply to Temporal's `none`/`local` (no resource caps, `os.environ` credential race under concurrent activities).

**Explicitly out of scope, documented as follow-ups**:
- `ensure_credentials_env()` (`step/provider.py`) mutates `os.environ` with no locking -- a real race under Temporal's concurrent activities sharing one worker process once `DirectExecutor` runs there. Pre-existing on the local runner if it ever runs workflows concurrently; worse on the Temporal worker (`MAX_CONCURRENT_ACTIVITIES=10` default).
- `spawn: local`'s `SubprocessExecutor` has no CPU/memory resource caps on the Temporal worker, unlike ephemeral sandbox `SpawnConfig` limits.
- `activities.py::run_sandbox_step`'s near-duplication of `core/step_runner.py::run_step()` (pre-existing, unrelated to this fix) -- separate de-dup issue.
- Retry-policy double-charge concern raised in review turned out not to apply: `DirectExecutor`/`SubprocessExecutor.run()` never raise -- every failure path returns `StepResult(status="failed", ...)`, which returns normally from the activity without triggering `RetryPolicy` (retries only fire on an actual raised exception), consistent with how the existing ephemeral path already handles `success=false` LLM-level failures without retrying.
- Adding the circuit breaker to the local engine for consistency -- Temporal-only for this fix.

**Effort**: 1 day (plus 3 rounds of design review before implementation, given replay-safety and behavioral-parity constraints)

### T59: `spawn: local` under `LocalWorkflowRunner`'s real orchestration untested ([issue #227](https://github.com/jameswnl/lightspeed-cloud-agents/issues/227)) -- Done (PR #232)

**Status**: Done

**Problem**: Surveying engine (`local`/`temporal`) x spawn-mode (`none`/`local`/`ephemeral`) x spawner test coverage found two gaps in the local engine's own orchestration path (a third finding, Temporal ignoring per-step `spawn`, was split out as #228/T58 above):
- Finding 2: no test ran a real LLM call through `LocalWorkflowRunner`'s actual pydantic-graph state machine (approval gates, context threading, condition evaluation) for `spawn: local` -- existing coverage was either graph-construction-only (`test_workflow_yaml.py`, no execution) or called `SubprocessExecutor` directly, bypassing `graph_translator.py`/`LocalWorkflowRunner` entirely (`test_allowed_skills.py`).
- Finding 3: `tests/integration/test_local_executor.py` mocker-patches `get_step_executor` itself, so it never exercises real dispatch to `DirectExecutor`/`SubprocessExecutor` through the actual factory.

**What was built**:
1. `tests/e2e/test_local_runner_real_dispatch.py`: drives `LocalWorkflowRunner.start()` end-to-end with `get_step_executor` unpatched (real factory dispatch), a real auto-approved approval gate, real `{{ steps.X.output.Y }}` context interpolation between steps, and a real LLM call for both `spawn: none` (`DirectExecutor`) and `spawn: local` (`SubprocessExecutor`, a real forked child process). Backed by a small in-memory `RunStateStore` stand-in (not a mock) so assertions run against `get_status()`'s real post-execution state, the same surface a caller of `LocalWorkflowRunner` actually uses.
2. Real production bug found and fixed by this new test: `subprocess_child.py::_parse_content` never got the markdown-fence-stripping fix (`_strip_markdown_fence`) that `direct.py::_parse_output` received for #188 -- so any `spawn: local` step with `output_schema` failed whenever the model wrapped its JSON in a ```` ```json ```` fence (gpt-4o-mini does this reliably). Added the same `_MARKDOWN_FENCE_RE`/`_strip_markdown_fence` helper to `subprocess_child.py`, applied at the same call site. 6 new unit tests in `tests/unit/workflow/executor/test_subprocess_child.py::TestParseContent`, mirroring `test_direct_executor.py::TestParseOutput`'s fence-stripping coverage.

**Effort**: 0.5 day

### T60: Test file naming/reorg cleanup (follow-up to T59) -- Done

**Status**: Done

**Problem**: While reviewing T59's new test, noticed `tests/e2e/` and `tests/integration/` inconsistently suffix filenames with `_e2e`/`_integration` even though the containing directory already encodes the tier (about half the files in each had the suffix, half didn't). Worse than naming noise: `tests/unit/workflow/executor/test_direct_integration.py` was a real integration test (spawn: none through `build_graph`, mocked LLM boundary) mislabeled and misplaced inside `tests/unit/`, and `tests/integration/test_workflow_yaml_e2e.py` was named like a real-LLM e2e test but is actually mocked-LLM -- the "e2e" and "integration" tiers were bleeding across all three directories.

**What was built**:
1. Dropped the redundant `_e2e`/`_integration` filename suffix on 7 files whose directory already says the tier: `test_allowed_skills.py`, `test_local_runner_real_dispatch.py`, `test_structured_output.py`, `test_triggers.py`, `test_workflow.py` (all `tests/e2e/`), `test_local_executor.py`, `test_workflow_yaml.py` (both `tests/integration/`). Used `git mv` to preserve history.
2. Moved `tests/unit/workflow/executor/test_direct_integration.py` -> `tests/integration/test_direct_executor_graph.py` (renamed for clarity in its new home, no content changes) -- it exercises `DirectExecutor` through the real `graph_translator.build_graph` with a mocked LLM, which is `tests/integration/`'s contract, not `tests/unit/`'s.
3. Updated all cross-references: `.github/workflows/e2e_tests.yaml` (2 hardcoded filenames), docstring cross-references in `test_direct_executor.py`/`test_allowed_skills.py`/`test_local_runner_real_dispatch.py`, and this file's T59 entry.
4. Verified: `pytest --collect-only` on `tests/unit`+`tests/integration` finds no import errors or duplicate test IDs; full `tests/unit` (1819 passed, 5 skipped) and `tests/integration` (70 passed -- up from 61, the 9 moved tests; same 9 pre-existing Temporal-server-required failures) both green; both renamed real-LLM e2e files re-verified passing under their new names.

**Effort**: 0.5 hour

### T60-followup: Remaining `_integration` suffix cleanup + stale `test_list.txt` -- Done

**Status**: Done

**Problem**: beesarmy's review of T60's PR flagged two things left incomplete: `tests/integration/test_identity_integration.py`, `test_policy_integration.py`, and `test_rbac_integration.py` still carried the redundant `_integration` suffix T60 was meant to sweep, and `tests/e2e/test_list.txt` (excluded from the original cross-reference grep, which only covered `.py`/`.md`/`.yaml`) still pointed at the pre-T60 names `test_workflow_e2e.py`/`test_triggers_e2e.py`.

**What was built**:
1. `git mv` renamed the three remaining files: `test_identity.py`, `test_policy.py`, `test_rbac.py`.
2. Fixed `test_list.txt`'s two stale filename references (landed directly on T60's branch before merge, so already reflected in `main`).
3. Verified no other live references (checked `.github/workflows/*.yaml`, `CLAUDE.md`, this file) point at the old three names -- only frozen historical review docs under `docs/archived/`/`docs/gaps/phase-2-records/` still mention them, left as-is since those are point-in-time snapshots, not living docs.
4. Verified: `pytest --collect-only` on the three renamed files finds all 17 tests with no import errors. Full `tests/unit`+`tests/integration` run on this branch matches the identical run on unmodified `main`: 1882 passed, 22 skipped, and the same 9 pre-existing `test_temporal_workflows.py` failures on both (require a live Temporal server; excluded from the "no regressions" claim, not fixed by this PR) -- no regressions introduced by the rename.

**Effort**: 15 minutes

### T61: `OpenShellSpawner` static bearer token can't refresh mid-spawn ([issue #236](https://github.com/jameswnl/lightspeed-cloud-agents/issues/236)) -- Done

**Status**: Done

**Problem**: `OpenShellSpawner._create_grpc_channel()` only ever accepted a static `bearer_token: str`, baked into the raw gRPC channel via `grpc.access_token_call_credentials(self._bearer_token)`. Reproduced live against a real OIDC-secured staging gateway (Keycloak, 300-second access-token TTL, confirmed by decoding the JWT's own `iat`/`exp` claims): the token expired mid-`spawn`, and cleanup (`_delete_provider`/`_detach_provider`/`_do_destroy`) then also failed reusing the same stale token, leaking both the sandbox and its attached credential provider. The issue was originally scoped around switching to the `openshell` SDK's `SandboxClient.from_active_cluster()` + `ClientCredentialsAuth` machinery, but investigation showed `_create_grpc_channel()` is already called fresh, inline, at every one of its ~5 call sites (never cached at spawner-construction time) -- so the actual gap was narrower: no *supported* way to swap in a fresh token per call, short of reaching into the private `_bearer_token` attribute directly.

**What was built**:
1. `OpenShellSpawner.__init__` gained `bearer_token_provider: Callable[[], str] | None = None`, mutually exclusive with the existing `bearer_token: str` (raises `ValueError` if both are set).
2. `_create_grpc_channel()` resolves the token once per call via `self._bearer_token_provider() if self._bearer_token_provider else self._bearer_token`, and both the TLS-required fail-closed check and `grpc.access_token_call_credentials(...)` use that resolved value -- giving per-RPC refresh with no async/interceptor machinery needed, since the method already rebuilds the channel fresh every call. The static-string path (message text, `access_token_call_credentials`-based call) is unchanged byte-for-byte.
3. `spawner/factory.py`'s `_build_openshell_spawner()` gained a matching `bearer_token_provider` param, forwarded to both `openshell.SandboxClient(bearer_token=...)` (already supports `str | Callable[[], str] | None` at the currently-pinned SDK version, `>=0.0.111` -- no dependency bump needed) and `OpenShellSpawner(bearer_token_provider=...)`.
4. No changes to the Temporal entrypoint's env-var reading -- a Python callable can't come from an env var; the actual OIDC-minting provider will be built and wired by `lightspeed-stack` (tracked as a separate, dependent issue there), which calls `build_spawner(bearer_token_provider=<their provider>)` directly.
5. 8 new tests: `tests/unit/spawner/test_openshell_spawner.py::TestBearerTokenProviderConstruction` (mutual exclusivity, storage, default) and 2 new methods on `TestCreateGrpcChannel` (`test_bearer_token_provider_used_for_fresh_token` -- a provider with `side_effect=["token-1","token-2"]` proves per-call refresh, not caching; `test_bearer_token_provider_without_tls_raises`); `tests/unit/spawner/test_factory.py::TestBuildSpawnerOpenShellBearerTokenProvider` (3 tests: forwarding, both-set-raises, neither-set-no-kwarg).
6. Independent opus final-gate review: PASS (verified token-resolution consistency between the TLS gate and the credential call, backward compatibility of the static-token path, and confirmed no leftover code from an earlier, abandoned `ClientCredentialsAuth`/`metadata_call_credentials` design).

**Explicitly out of scope, documented as follow-ups**:
- The actual OIDC client-credentials token-minting/caching provider implementation -- lives in `lightspeed-stack`'s `SpawnerConfiguration`/`spawner_factory.py`, filed as a separate dependent issue there, blocked on this landing first.

**Effort**: 0.5 day

### T62: `spawn: ephemeral` can never resolve LLM inference credentials ([issue #238](https://github.com/jameswnl/lightspeed-cloud-agents/issues/238)) -- Done

**Status**: Done

**Problem**: `OpenShellSpawner._create_provider()` hardcoded `type="cloud-agents"` for every credential provider it creates. The OpenShell gateway's `SetInferenceRoute` RPC -- the call that registers a provider's credentials as resolvable for LLM inference -- rejects that type outright (`INVALID_ARGUMENT: provider has unsupported type 'cloud-agents' for cluster inference (supported: openai, anthropic, nvidia, deepinfra, google-vertex-ai, aws-bedrock)`), and `cloud_agents` never called `SetInferenceRoute` anywhere. So the sandbox's own `GetInferenceBundle` lookup (how the sandboxed agent learns "call this LLM API with this key") had nothing to resolve, ever, for any ephemeral spawn -- reproduced live against a local Kind gateway: the sandbox's supervisor polled `GetInferenceBundle` every ~5s, got gRPC `NOT_FOUND` every time, and the step reported `success: false` with no diagnostic detail. Every real-world use of `spawn: ephemeral` (i.e. one that expects the sandboxed agent to actually call an LLM) was silently broken.

**What was built**:
1. `_create_provider()` gained a `provider_type: str` parameter, forwarded to `datamodel_pb2.Provider(type=provider_type, ...)` instead of the hardcoded literal. (Originally shipped with a `= "cloud-agents"` default for backward compat; removed in item 7 below once that fallback was found to be both unreachable and meaningless.)
2. New `_set_inference_route(provider_name, model_id)` method: calls `SetInferenceRoute(provider_name=..., model_id=..., workspace=self._workspace)` via the `openshell.inference.v1.Inference` gRPC service.
3. `_do_spawn()`'s credential-provider block now reads `LIGHTSPEED_PROVIDER`/`LIGHTSPEED_MODEL` from the `env` dict it already receives (always set by `step_runner.run_step()`, the only caller that passes `credential_secret_name`) as the real `provider_type`/`model_id`, passes the real type to `_create_provider()`, and calls `_set_inference_route()` right after. (Superseded by item 7 below: this originally *skipped* `_set_inference_route()` silently when the provider type defaulted to "cloud-agents" or the model was missing; it now fails loudly instead -- see item 7 for the current, actual behavior.) A `_set_inference_route()` failure fails the whole spawn (same exception path as provider-creation failure) -- fail fast and loud rather than let the sandbox discover it 30+ seconds later via repeated `GetInferenceBundle` polling.
4. Proved the root cause and the fix directly against a real Kind gateway before writing any code: manually created a `type="cloud-agents"` provider and called `SetInferenceRoute` on it (rejected with the exact `INVALID_ARGUMENT` above), then repeated with `type="openai"` (succeeded, gateway validated the real `https://api.openai.com/v1/chat/completions` endpoint).
5. 10 new tests, later superseded by items 7-9 as behavior changed (see those items for the surviving test names): `TestProviderResponseMessages` gained a real-provider-type test and a since-removed "defaults to cloud-agents" test; new `TestSetInferenceRoute` (2 tests: correct gRPC fields, gateway-rejection propagation); new `TestDoSpawnInferenceRouteWiring` (4 tests, since rewritten -- route set when both provider+model present, skip-when-missing tests later replaced with fail-loud tests, spawn fails when route setup fails). Updated `test_spawn_with_credential_does_not_expose_real_value`'s assertion to include the now-forwarded `provider_type`.
6. **Follow-up from CodeRabbit review on PR #239**: the first version of `_set_inference_route()` omitted `route_name`, meaning it registered/overwrote a single shared *workspace-default* route -- concurrent ephemeral spawns in the same workspace would clobber each other's routes, and an earlier sandbox could resolve a later spawn's provider via `GetInferenceBundle`. Fixed by passing `route_name=provider_name` (already gateway-assigned and unique per spawn, no new state needed) so each spawn gets its own named route instead. Added a matching `_delete_inference_route()` + `destroy()`-time cleanup (mirroring the existing `_detach_provider` pattern, tracked via a new `_inference_route_names` dict, best-effort/log-and-continue on failure) so routes don't accumulate unboundedly in the workspace. 8 more tests (2 for `_set_inference_route`'s `route_name`/`_delete_inference_route`'s fields, 3 for `destroy()` cleanup, 1 tracking assertion added to an existing test).
7. **Follow-up: removed the `"cloud-agents"` fallback type entirely.** Review found that `provider_type = env.get("LIGHTSPEED_PROVIDER") or "cloud-agents"` fell back to a type that is never defined anywhere -- not by `cloud_agents`, not by OpenShell itself (`CreateProvider` accepts any string for `type`, but nothing else in either codebase reads or validates it beyond that). It was also never actually reachable in production: `LIGHTSPEED_PROVIDER`/`LIGHTSPEED_MODEL` are always set by every real caller of `credential_secret_name` (`step_runner.run_step()`, the Temporal executor's `activities.py`). Separately, `LIGHTSPEED_PROVIDER`'s real values (from `lightspeed-agentic-sandbox`'s own vendor list: `anthropic`, `openai`, `vertex`, `azure`, `bedrock`, `watsonx`) don't line up 1:1 with OpenShell's `SetInferenceRoute`-accepted types (`openai`, `anthropic`, `nvidia`, `deepinfra`, `google-vertex-ai`, `aws-bedrock`) -- the old code passed `LIGHTSPEED_PROVIDER` straight through unmapped, which happened to work for `openai`/`anthropic` (identical strings) but was silently wrong for `vertex`/`bedrock` and would have hit an opaque gateway `INVALID_ARGUMENT` for `azure`/`watsonx`. Fixed by: (a) adding a real `_INFERENCE_PROVIDER_TYPE_MAP` translation table + `_resolve_inference_provider_type()` static method mapping sandbox vendor identifiers to OpenShell's vendor types (`vertex`->`google-vertex-ai`, `bedrock`->`aws-bedrock`, identity for the rest, no entry at all for `azure`/`watsonx`); (b) making `_create_provider()`'s `provider_type` parameter required with no default; (c) `_do_spawn()` now raises `ValueError` immediately -- before any gRPC call -- if `LIGHTSPEED_PROVIDER` or `LIGHTSPEED_MODEL` is missing, or if `LIGHTSPEED_PROVIDER` has no known OpenShell equivalent, instead of silently creating an unusable provider or reaching the gateway only to get a generic rejection. Net effect: failures now surface immediately with a specific, actionable message, at our own layer, rather than as either a silent no-op or an opaque downstream gRPC error. Test changes: removed the now-obsolete "defaults to cloud-agents" test (replaced with one asserting `provider_type` is a required parameter); rewrote the "skips route when model/provider missing" tests to assert a loud `ValueError` instead of a silent skip; replaced the "azure reaches the gateway and is rejected there" test with one proving azure is rejected at the translation layer, before any gRPC call is attempted; added a new `TestResolveInferenceProviderType` class covering every mapped/unmapped vendor identifier.
8. **Follow-up: reverted item 6's `route_name=provider_name` -- it broke every spawn outright.** Human review on PR #239 flagged that item 6's named-route change diverged from the actually Kind-proven call shape (no `route_name`) and from the public SDK's `InferenceRouteClient.set_route()` (also omits it), and asked for either a revert or live re-verification before merging -- "Do not ship named routes on mocked gRPC field assertions alone." Live-verified against the real Kind gateway: `SetInferenceRoute` with `route_name="<gateway-assigned-provider-name>"` is rejected outright with `INVALID_ARGUMENT: unknown route_name '<x>'; expected 'inference.local' or 'sandbox-system'` -- `route_name` is not a free-form per-spawn identifier at all, it is a fixed two-value enum (a "user" route and a separate "system" route, confirmed via `openshell inference get` showing exactly these two sections). Item 6's fix was therefore never functional -- it would have failed 100% of real spawns, not just the "unverified concurrent-clobber" edge case the T62 doc previously flagged. Reverted `_set_inference_route()` to omit `route_name`; removed `_delete_inference_route()`, the `_inference_route_names` tracking dict, and destroy()-time route cleanup entirely, since there is exactly one shared workspace-level route and no per-sandbox route to delete -- deleting "the" route on any single sandbox's teardown would have broken every other concurrently active sandbox relying on it. Re-verified live end-to-end with the revert applied: `SetInferenceRoute` (no `route_name`) succeeds, and the spawned sandbox's own `GetInferenceBundle` calls immediately succeed (HTTP 200, no error) right after boot -- confirming the core #238 fix (the sandbox can resolve an inference route at all) holds with the revert. **The concurrent-spawn route-clobbering concern from item 6 is real, still open, and currently has no client-side fix**: with only one shared route per workspace, two ephemeral spawns running at the same time in the same workspace will overwrite each other's credentials/model. This needs either a different gateway-side mechanism (e.g. a real per-sandbox-scoped route once the gateway supports one) or a workspace-per-spawn strategy; out of scope for this PR. Also fixed in the same follow-up: a provider-leak bug found in the same review -- `_set_inference_route()` failing (e.g. an unsupported-but-mapped type, or a transient gateway error) left the just-created credential-bearing provider undeleted, since that failure happens before sandbox creation and the post-create cleanup path never runs. Now explicitly deletes the provider (best-effort, preserving the original exception) before re-raising. Test changes: removed all `route_name=`/`_delete_inference_route`/destroy-time-route-cleanup tests and assertions added in item 6; added a provider-cleanup assertion to the existing route-setup-failure test plus a new test for "cleanup itself also fails, original error still propagates".

9. **Follow-up: addressed 3 nits from the PR #239 approval review.** (a) Deleted the entire deprecated dead-code chain -- `_inject_credentials()`, `_inject_credentials_via_files()`, `_create_and_attach_provider()` -- rather than just patching its remaining hardcoded `type="cloud-agents"` literal: confirmed via grep it had zero production callers (only reachable from each other and from `TestCredentialInjection`'s tests; `_do_spawn` never calls it), so it was the "last remaining copy" of the exact bug this whole issue fixed, and this codebase's convention is to delete confirmed-dead code rather than patch it in place. Removed its tests too; updated `test_provider_uses_datamodel_module`'s regression count (2 -> 1 `datamodel_pb2.Provider` use, since only `_create_provider` remains). (b) Tightened this doc's item 3/5 wording, which described pre-item-7/8 behavior in the present tense. (c) Investigated the suggestion to add `_PROVIDER_HOSTS`'s `"claude"`/`"gemini"` aliases to `_INFERENCE_PROVIDER_TYPE_MAP` for consistency -- **did not add them**: confirmed directly against `lightspeed-agentic-sandbox/src/lightspeed_agentic/config.py`'s `resolve_sdk()` that `LIGHTSPEED_PROVIDER` only ever accepts `anthropic`, `vertex`, `openai`, `azure`, `bedrock` as *input* (raises `ValueError: Unknown provider` for anything else, explicitly listing that same set as "Supported"); `"claude"`/`"gemini"` are only ever the *resolved* internal SDK name (`ResolvedSDK.name`), never a legal `LIGHTSPEED_PROVIDER` value. So `LIGHTSPEED_PROVIDER=claude` already crashes the sandbox itself before any inference-routing code runs, on both HEAD and any hypothetical fix here -- adding it to the inference map would only make our own fail-loud check silently accept a value guaranteed to fail one layer deeper, which is worse, not better. `_PROVIDER_HOSTS`'s `"claude"`/`"gemini"` egress entries appear to be the actually-stale ones (pre-existing, not touched by this PR).

**Secondary wrinkle, not fully explained**: while manually reproducing this live, `GetInferenceBundle` succeeded for a couple of calls right after a successful `SetInferenceRoute`, then started failing with `NOT_FOUND` again a few seconds later with no new `SetInferenceRoute` call and nothing that should have invalidated the route. Re-observed during item 8's live re-verification too, but now explained: it correlates with the sandbox/provider actually being torn down shortly after (the K8s pod's supervisor keeps polling for ~30s during graceful termination after `DeleteSandbox`/`DetachSandboxProvider` already ran) -- not a spontaneous, unexplained route invalidation. Not a concern for a live (non-destroyed) sandbox.

**New, separate issue found during item 8's live re-verification -- not fixed here**: even with `GetInferenceBundle` resolving successfully server-side, the actual `/v1/agent/run` query to the live sandbox failed with the *application-level* error `"Agent error: Missing credentials... set the OPENAI_API_KEY environment variable"`. The gateway's own logs show why: `provider type has no profile; skipping provider policy layer` for our ad-hoc `CreateProvider`-created provider. This suggests OpenShell's provider-to-environment-variable injection for a running sandbox process requires a registered "provider profile" (see the `openshell provider profile`/`list-profiles` CLI subcommands) that `cloud_agents` never creates -- a plain `CreateProvider` + `spec.providers` attachment with no profile may never actually inject `OPENAI_API_KEY` (or equivalent) into the exec'd process's environment, regardless of whether inference routing itself works. This looks pre-existing and orthogonal to both #238's route-resolution bug and item 8's route_name revert (neither of which touches provider profiles), but it means "the sandbox can actually make a real LLM call end-to-end" is *still* not proven live even after this PR. Needs its own investigation/issue before treating `spawn: ephemeral` + real credentials as fully working.

**Effort**: 0.5 day
