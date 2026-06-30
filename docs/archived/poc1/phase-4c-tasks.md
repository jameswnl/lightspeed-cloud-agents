# Phase 4c: Observability & Advanced Features — Task Breakdown

## Context

Phases 1-4b established the cloud agents platform: generic runtime, workflow executor with conditions/retry/approval/auto-approve, PostgreSQL persistence, on-demand spawning, and auth middleware. Phase 4c adds observability (traces, metrics, streaming) and advanced workflow features (parallel execution, MCP tools, advisory mode, notifications, AI workflow generation).

**Approach:** TDD — write tests first, then implement. Every task gets an independent opus evaluator.
**Reviewer polling:** Active — reviewer writes findings to `docs/design/cloud-agents/`.

### Task independence

Items are **product-level optionality** (any can be deferred without blocking others), but **not implementation-independent**. Several tasks share critical files:

- **Executor-centric cluster** (Tasks 3, 4, 6, 7, 8, 9, 10): All hook into `executor.py` state transitions. Implement in listed order to avoid merge conflicts.
- **Runtime cluster** (Tasks 2, 5, 9): All modify `generic_runner.py` agent construction. Task 2 first, then 5, then 9.
- **Standalone** (Tasks 1, 11): Truly independent — can be implemented in any order.

Within each cluster, tasks should be implemented sequentially. Across clusters, tasks can run in parallel.

---

## Implementation Order (11 tasks)

### Task 1: Nested Path Interpolation (Item 20)

**Why first:** Pure data-transformation, zero dependencies, unlocks parallel steps and AI designer.

**Current state:** `src/agents/workflow/interpolation.py` uses regex `r"\{\{\s*steps\.(\w+)\.output\.(\w+)\s*\}\}"` — single-level only. Cannot do `{{ steps.X.output.details.host }}` or `{{ steps.X.output.items[0].name }}`.

**Design:**
- New `resolve_path(data, path)` helper that walks dot-separated keys and `[N]` array indices
- Extended regex to capture full dotted paths with bracket indexing after `output.`
- Backward-compatible: `steps.X.output.Y` still works
- No arbitrary expressions, slicing, or negative indices (PoC scope)
- `ValueError` on invalid paths (consistent with existing behavior)

**Files:**
- Modify: `src/agents/workflow/interpolation.py` — add `resolve_path()`, update regex/`interpolate()`
- Modify: `tests/unit/agents/workflow/test_interpolation.py` — add `TestNestedInterpolation`, `TestResolvePath`

**Production gaps:** No wildcard paths (`items[*].name`), no negative indices, no slicing.

---

### Task 2: Per-Tool Prometheus Metrics (Item 13)

**Current state:** `src/agents/runtime/metrics.py` has `ls_agent_runs_total` and `ls_agent_run_duration_seconds` (per-run). No per-tool metrics.

**Design:**
- New metrics: `ls_agent_tool_calls_total` (Counter, labels: agent_name, tool_name, status), `ls_agent_tool_duration_seconds` (Histogram, labels: agent_name, tool_name)
- New `instrument_tool(fn, agent_name, tool_name)` wrapper that records metrics around each tool call
- Applied in `generic_runner.py` when registering tools via `agent.tool_plain()`

**Files:**
- Modify: `src/agents/runtime/metrics.py` — add new metric objects
- Create: `src/agents/runtime/tool_instrumentation.py` — `instrument_tool()` wrapper
- Modify: `src/agents/runtime/generic_runner.py` — wrap tools with instrumentation
- Create: `tests/unit/agents/runtime/test_tool_instrumentation.py`

**Production gaps:** No per-tool-per-run correlation. No MCP tool call metrics yet.

---

### Task 3: Advisory Mode (Item 16)

**Current state:** No concept of read-only mode. `WorkflowStepSpec` has `type: Literal["agent", "human-approval"]` only.

**Design:**
- Add `mode: Literal["standard", "advisory"] = "standard"` to `WorkflowDefinition.metadata` (workflow-level default)
- **Two-layer enforcement:**
  1. **Tool filtering (hard enforcement):** In advisory mode, tools are classified as read-only or write-capable via a `tool_classification` map in the agent definition. Write-capable tools are removed from the agent before execution. If no classification exists, all tools are allowed but a warning is logged.
  2. **Prompt annotation (soft enforcement):** Agent prompts get `"\n\nADVISORY MODE: Diagnose only. Report what you would do without taking action."` appended as a secondary signal.
- Advisory workflows skip all `human-approval` steps (nothing to approve)
- Step results get `advisory: true` in output dict
- `AdvisoryEnforcer` class handles both tool filtering and prompt annotation
- Tool classification in `agent.yaml`:
  ```yaml
  tools:
    module: agents.tools.diagnostic_tools
    functions: [list_hosts, check_host, run_remediation]
    read_only: [list_hosts, check_host]
  ```

**Files:**
- Modify: `src/agents/workflow/definition.py` — add `mode` to metadata handling
- Modify: `src/agents/definition.py` — add `read_only` field to `ToolsSpec`
- Create: `src/agents/workflow/advisory.py` — `AdvisoryEnforcer` class with tool filtering + prompt annotation
- Modify: `src/agents/workflow/executor.py` — check advisory mode before steps, pass tool filter to runner
- Modify: `src/agents/runtime/generic_runner.py` — accept tool filter set
- Create: `tests/unit/agents/workflow/test_advisory.py`
- Modify: `tests/unit/agents/workflow/test_executor.py` — add `TestWorkflowExecutorAdvisory`

**Production gaps:** Tool classification is manual per-agent. Production could auto-classify based on tool naming conventions or static analysis of tool side effects.

---

### Task 4: OpenTelemetry Distributed Tracing (Item 11)

**Current state:** OTel SDK/OTLP exporter in pyproject.toml dev deps. Correlation IDs generated but no OTel spans.

**Design:**
- Centralized `src/agents/runtime/tracing.py` with `init_tracing(service_name)` and `get_tracer(name)`
- `OTEL_EXPORTER_OTLP_ENDPOINT` env var for configuration; `NoOpTracerProvider` when unset
- Instrument three layers:
  1. Agent runtime (`server.py`): span on `/v1/run` with `agent.name`, `correlation.id`
  2. Workflow executor: parent span per workflow, child spans per step
  3. RemoteAgentClient: propagate `traceparent` header (W3C Trace Context)
- Span naming: `agent.run.<name>`, `workflow.<name>`, `workflow.step.<name>`

**Files:**
- Create: `src/agents/runtime/tracing.py` — tracer init, context propagation
- Modify: `src/agents/runtime/server.py` — add spans to run handlers
- Modify: `src/agents/workflow/executor.py` — add spans to workflow/step execution
- Modify: `src/agents/remote_agent_client.py` — inject `traceparent` header
- Modify: `src/agents/runtime/generic_entrypoint.py` + `src/agents/workflow/entrypoint.py` — call `init_tracing()`
- Create: `tests/unit/agents/runtime/test_tracing.py` — use `InMemorySpanExporter`

**Production gaps:** No sampler config, no baggage propagation, no OTLP TLS/auth, no FastAPI middleware-level instrumentation.

---

### Task 5: MCP Tools in agent.yaml (Item 14)

**Current state:** `AgentSpec.tools` only supports Python-importable tools (`ToolsSpec(module, functions)`). pydantic-ai already supports `MCPServerHTTP` tools.

**Design:**
- Add `MCPServerSpec` model: `name`, `url`, `auth` (optional)
- Auth model supports two modes:
  - `env_var`: references an environment variable name (e.g., `MCP_AUTH_TOKEN`) — the value is read at runtime, never stored in YAML
  - `header_value`: inline header value — **dev/test only**, logged with a warning at startup
- Add `mcp_servers: Optional[list[MCPServerSpec]]` to `AgentSpec` in `definition.py`
- New `load_mcp_servers(specs)` in `mcp_loader.py` — creates `MCPServerHTTP` instances, resolves auth
- `generic_runner.py` passes MCP tools to `Agent()` constructor alongside plain tools
- Lifecycle managed via FastAPI lifespan (connect/disconnect)
- Config example:
  ```yaml
  mcp_servers:
    - name: cluster-tools
      url: http://mcp-server:8080
      auth:
        type: env_var
        env_var: MCP_CLUSTER_TOKEN
        header_name: Authorization
        header_prefix: "Bearer "
  ```

**Files:**
- Modify: `src/agents/definition.py` — add `MCPServerSpec`, `MCPAuthSpec`, `mcp_servers` field
- Create: `src/agents/runtime/mcp_loader.py` — loader + lifecycle + auth resolution
- Modify: `src/agents/runtime/generic_runner.py` — accept MCP tools
- Modify: `src/agents/runtime/generic_entrypoint.py` — manage MCP lifecycle in lifespan
- Create: `tests/unit/agents/runtime/test_mcp_loader.py`

**Production gaps:** No MCP server health checks, no tool allow/deny list, no retry/reconnect. Production should use K8s secrets mounted as env vars (supported by the `env_var` auth mode). No mTLS support.

---

### Task 6: SSE Streaming for Agent Progress (Item 15)

**Current state:** Existing SSE patterns in `responses.py` and `a2a.py`. Agent runtime returns sync/async but no streaming. Workflow executor has no event emission.

**Design:**
- `WorkflowEvent` model (Pydantic) with types: `workflow.started`, `step.started`, `step.completed`, `step.failed`, `step.skipped`, `workflow.paused`, `workflow.completed`, `workflow.failed`
- Executor accepts optional `event_callback: Callable[[WorkflowEvent], Awaitable[None]]`
- SSE endpoint `POST /v1/workflows/run/stream` creates `asyncio.Queue`, feeds events to `StreamingResponse`
- Agent runtime gets `POST /v1/run/stream` for agent-level streaming

**Files:**
- Create: `src/agents/workflow/events.py` — `WorkflowEvent` model
- Modify: `src/agents/workflow/executor.py` — emit events at state transitions
- Modify: `src/agents/workflow/api.py` — add SSE endpoint
- Modify: `src/agents/runtime/server.py` — add `/v1/run/stream`
- Create: `tests/unit/agents/workflow/test_events.py`

**Production gaps:** No SSE heartbeat, no `Last-Event-ID` reconnection, no event persistence for replay.

---

### Task 7: Approval via Slack/Webhook (Item 12)

**Current state:** Approval is HTTP POST to `/v1/workflows/{id}/approve`. No external notifications.

**Design:**
- `ApprovalNotifier` protocol with `SlackNotifier`, `WebhookNotifier`, `NullNotifier`
- Add `ApprovalNotifierSpec` to `WorkflowStepSpec`: `type: Literal["slack", "webhook"]`, `url`, `channel` (optional)
- Fire-and-forget notification when workflow pauses for approval
- Actual approval still via HTTP POST (notifier is notification, not transport)

**Files:**
- Create: `src/agents/workflow/notifier.py` — protocol + implementations
- Modify: `src/agents/workflow/definition.py` — add `ApprovalNotifierSpec`
- Modify: `src/agents/workflow/executor.py` — call notifier on pause
- Create: `tests/unit/agents/workflow/test_notifier.py`

**Production gaps:** No Slack interactive messages (needs OAuth app), no webhook HMAC, no timeout notification.

---

### Task 8: Escalation Packaging (Item 17)

**Current state:** `EscalationHandoff` model exists in `retry.py`. Returns structured dict but doesn't send it anywhere.

**Design:**
- `EscalationPackager` protocol with `JiraPackager`, `WebhookPackager`, `LogPackager` (default)
- Called from executor after `build_escalation()`
- Package includes: escalation handoff, workflow state snapshot, collected evidence, correlation ID
- `EscalationConfig` in `WorkflowDefinition.metadata`: `type`, `url`, `project_key`

**Files:**
- Create: `src/agents/workflow/escalation.py` — protocol + implementations
- Modify: `src/agents/workflow/executor.py` — call packager after escalation
- Modify: `src/agents/workflow/definition.py` — add `EscalationConfig`
- Create: `tests/unit/agents/workflow/test_escalation.py`

**Production gaps:** No Jira OAuth, no PagerDuty/ServiceNow, no deduplication, no link-back to workflow.

---

### Task 9: Per-Task Permission Scoping (Item 18)

**Current state:** K8s spawner uses single ServiceAccount. No per-step permissions.

**Design:**
- `PermissionScope` model: `service_account`, `allowed_tools`, `denied_tools`, `max_tokens`, `timeout_seconds`
- Add `permissions` to `WorkflowStepSpec`
- Executor passes permissions to spawner (ServiceAccount) and in request context (tool filtering)
- `generic_runner.py` filters tools based on `allowed_tools`/`denied_tools` from request context

**Files:**
- Create: `src/agents/workflow/permissions.py` — model + validation
- Modify: `src/agents/workflow/definition.py` — add `permissions` field
- Modify: `src/agents/workflow/executor.py` — pass permissions
- Modify: `src/agents/runtime/generic_runner.py` — tool filtering
- Modify: `src/agents/spawner/kubernetes_spawner.py` — per-step ServiceAccount
- Create: `tests/unit/agents/workflow/test_permissions.py`

**Production gaps:** Permission enforcement is split (advisory in executor, actual in K8s RBAC). No OPA policy language. No per-user scoping.

---

### Task 10: Parallel Step Execution (Item 19)

**Current state:** Sequential `for i in range(...)` loop in `executor._execute_from()`.

**Design:**
- Add `parallel_group: Optional[str]` to `WorkflowStepSpec`
- Steps with same `parallel_group` run concurrently via `asyncio.gather()`
- Steps without `parallel_group` run sequentially (backward compatible)
- `group_steps()` partitions step list into sequential and parallel batches
- Validation: no cross-references within a group, no approval steps in groups
- `parallel_fail_strategy: Literal["fail-fast", "continue"] = "fail-fast"`

**Parallel group invariants:**
1. **Barrier semantics:** Later steps only run after the entire parallel group reaches a terminal state (all completed, or failed per strategy). No step after the group sees partial results.
2. **Output representation:** Each step in the group writes to its own `output_key`. On `fail-fast`, failed steps have `status="failed"`, succeeded steps have `status="completed"`, cancelled steps have `status="cancelled"`. On `continue`, all steps run to completion regardless of failures.
3. **Side-effect safety:** Steps within a parallel group are assumed to target independent systems. The executor does NOT enforce isolation — this is the workflow author's responsibility. Definition-time validation warns (but does not block) if two steps in a group target the same agent.
4. **Retry within parallel groups:** Each step retries independently per its `max_retries`. A step exhausting retries triggers group failure under `fail-fast`.
5. **Spawner cleanup:** On `fail-fast` cancellation, spawned pods for cancelled steps are destroyed in the `finally` block (reusing the existing cleanup pattern).

**Files:**
- Modify: `src/agents/workflow/definition.py` — add fields
- Create: `src/agents/workflow/parallel.py` — `group_steps()`, `validate_parallel_groups()`
- Modify: `src/agents/workflow/executor.py` — refactor `_execute_from()` for batch execution, add `_execute_parallel_batch()`
- Create: `tests/unit/agents/workflow/test_parallel.py`
- Modify: `tests/unit/agents/workflow/test_executor.py` — `TestWorkflowExecutorParallel`

**Production gaps:** No dynamic parallelism, no resource limits per parallel group, no DAG execution. No automatic side-effect isolation between parallel steps.

---

### Task 11: AI-Generated Workflows — Workflow Designer Agent (Item 21)

**PoC scope:** Agent takes natural language description, generates valid `WorkflowDefinition` YAML. Human reviews before execution.

**Design:**
- New agent module at `src/agents/designer/` following `diagnostic/`/`monitoring/` pattern
- Output type: `WorkflowDesign` model (workflow YAML + rationale + validation_status)
- Designer tools:
  - `list_available_agents()` — reads registry
  - `get_agent_capabilities(agent_name)` — returns tools and output type
  - `validate_workflow(yaml_str)` — parses via `WorkflowDefinition.model_validate()`
  - `list_workflow_features()` — returns available syntax/features
- System prompt includes `WorkflowDefinition` schema and examples
- Agent definition YAML at `agents/definitions/workflow-designer.yaml`

**Draft lifecycle:**
1. **Generate:** Designer agent produces `WorkflowDesign` with YAML + rationale
2. **Validate:** `validate_workflow()` tool is called by the agent before returning — output includes `validation_status: Literal["valid", "invalid"]` and any validation errors
3. **Persist draft:** The workflow API stores the draft via a new `POST /v1/workflows/drafts` endpoint. Drafts are stored in the same persistence backend (PostgreSQL/file/memory) with `status: "draft"`. Each draft gets a unique `draft_id`.
4. **Review:** `GET /v1/workflows/drafts/{draft_id}` returns the YAML and rationale for human review
5. **Approve or reject:** `POST /v1/workflows/drafts/{draft_id}/approve` promotes the draft to an executable workflow definition. `POST /v1/workflows/drafts/{draft_id}/reject` marks it as rejected.
6. **Execute:** Only approved drafts can be run via `POST /v1/workflows/run`

**Files:**
- Create: `src/agents/designer/__init__.py`, `agent.py`, `tools.py`, `models.py`, `entrypoint.py`
- Create: `agents/definitions/workflow-designer.yaml`
- Modify: `src/agents/workflow/api.py` — add draft CRUD endpoints
- Modify: `src/agents/workflow/persistence.py` — add draft storage methods
- Create: `tests/unit/agents/designer/__init__.py`, `test_tools.py`, `test_agent.py`

**Production gaps:** No iterative refinement, no cost estimation, no version control integration, no constraint language, no RBAC on generation. Draft storage is same-backend as workflow state (production may want a separate config store).

---

## Critical Files (touched by multiple tasks)

| File | Tasks |
|------|-------|
| `src/agents/workflow/executor.py` | 3, 4, 6, 7, 8, 9, 10 (7 tasks) |
| `src/agents/workflow/definition.py` | 3, 7, 8, 9, 10 (5 tasks) |
| `src/agents/runtime/generic_runner.py` | 2, 5, 9 (3 tasks) |
| `src/agents/runtime/server.py` | 4, 6 (2 tasks) |
| `src/agents/definition.py` | 5, 9 (2 tasks) |

---

## Verification

**Per-task:**
```bash
uv run pytest tests/unit/agents/ -q          # all agent tests pass
uv run make format                            # auto-format
uv run make verify                            # all linters pass
```

**End-to-end (after all tasks):**
- Deploy with `docker-compose.generic-agents.yaml`
- Verify OTel traces appear in collector (or Jaeger)
- Verify new Prometheus metrics at `/metrics`
- Verify SSE streaming via `curl -N`
- Run advisory-mode workflow, confirm no remediation taken
- Run parallel-step workflow, confirm concurrent execution
- Generate a workflow via the designer agent, validate the output YAML
