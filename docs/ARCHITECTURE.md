# Cloud Agents Framework — Architecture

## Overview

The Cloud Agents Framework is an **agent/workflow orchestration platform** built into lightspeed-stack. It enables product teams to create, deploy, and manage AI agents and multi-step workflows as server-side services in customer clusters.

The framework uses **Temporal** for durable workflow execution and **lightspeed-agentic-sandbox** for isolated agent execution. Each workflow step runs in a disposable sandbox container using the **OpenAI agents SDK** — no framework code changes required to add new agent types.

## Goals & Objectives

1. **Bring your own agents & workflows** — define agents and multi-step agentic workflows via YAML + any tools. No forking, no rebuilds, no framework changes. Product teams deploy AI agents without changing framework code.

2. **Secured & governed execution** — each step runs in its own disposable container with scoped permissions, hard timeouts, and no shared state. Untrusted pods never receive secrets beyond their API token. Human oversight on high-risk operations via approval gates. Full observability: tracing, metrics, event streaming.

3. **Composable agent ecosystem** — agents and workflows are reusable building blocks. A chatbot invokes workflows as tools. Workflows chain agents. Multiple trigger points: conversations, alerts, API, schedules.

4. **Seamless human-agent handoff** — when automation reaches its limit, users pick up in an AI CLI with full workflow context — diagnosis, steps taken, failure history — and continue where the agent left off.

5. **Dual deployment: Kubernetes + Podman** — same agents run on both. Both are first-class production targets.

## Design Principles

### Framework, not pre-built agents

The diagnostic and monitoring agents are **examples**, not the product. The framework provides:
- Generic sandbox image (one image for all agent types)
- Temporal workflow engine with conditions, retry, approval, parallel steps
- Spawner abstraction (K8s Jobs / Podman containers)
- Durable execution via Temporal Server
- Observability (tracing, metrics, events)

Product teams provide:
- `agent.yaml` — instructions, tools, output type, lifecycle
- `tools.py` — Python functions the agent can call
- `workflow.yaml` — multi-step workflow definition
- `skills/` — domain knowledge packages (optional)

### Ephemeral-by-default execution

Every workflow step spawns a fresh container. The container:
- Starts clean — no state from previous steps
- Has only the tools configured for this agent type
- Has hard timeouts — killed automatically if it hangs
- Has scoped permissions — only what the workflow author declares
- Is destroyed after execution — no cleanup worries

This means a stuck LLM call can't block the workflow runner, a misbehaving agent can't crash the platform, and each step is isolated from every other step.

Pre-deployed (long-running) agents are supported via `spawn: pre-deployed` for cases where startup latency matters more than isolation.

### Durable execution via Temporal

The workflow runner delegates all state management to Temporal Server. This enables:
- **Horizontal scaling** — multiple worker replicas behind a Service/LB
- **Pod resilience** — any replica crashes, Temporal re-dispatches activities to healthy workers
- **Cross-replica operations** — start on replica A, approve on replica B via Temporal signals
- **Automatic retry** — Temporal handles step retry with configurable `RetryPolicy`
- **Timeout enforcement** — Temporal kills activities that exceed `start_to_close_timeout`

No optimistic locking, no recovery poller, no PostgreSQL — Temporal provides all of these natively.

### Dual deployment: Kubernetes and Podman

Both deployment targets provide **behavioral parity** (same features) but different **security mechanisms**:

| Capability | Kubernetes | Podman |
|-----------|-----------|--------|
| Ephemeral spawning | K8s Jobs + Services | Podman containers + port mapping |
| Networking | K8s Services + ClusterIP DNS | Podman network + container DNS |
| RBAC | ServiceAccounts + RoleBindings | OS-level access control |
| NetworkPolicy | Enforced by CNI | Host firewall rules |
| Durable execution | Temporal Server (PVC or external) | Temporal Server (local or external) |
| Config distribution | Env vars + K8s Secrets | Env vars |

Deployers compensate for Podman's lack of K8s-native security with host-level controls. The spawner abstraction (`AgentSpawner` ABC) hides the deployment target from the workflow engine.

### Human-in-the-loop by design

Following KubeKlaw learnings, the framework enforces phased execution:

1. **Diagnose** — gather evidence, identify root cause
2. **Propose** — present options with risk levels and rollback plans
3. **Gate** — human reviews and approves (or auto-approve for low-risk)
4. **Execute** — carry out the approved plan
5. **Verify** — independently confirm the fix worked

Policy-driven approval classifies steps by risk:
- **Low risk** (analysis, verification) → auto-approve
- **High risk** (execution, remediation) → require human approval

The approval routing design supports pluggable channels (Slack, webhook, conversational) with RBAC-scoped approvers.

## Architecture Components

```
┌─────────────────────────────────────────────────────────────┐
│  K8s Cluster / Podman Host                                  │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │  Platform Framework  │    │  Sandbox Pods (per step)    │ │
│  │                     │    │                             │ │
│  │  Workflow Runner    │───▶│  lightspeed-agentic-sandbox │ │
│  │  ├─ Temporal Worker │    │  ├─ OpenAI agents SDK       │ │
│  │  ├─ Spawner         │    │  ├─ POST /v1/agent/run      │ │
│  │  └─ Definition Store│    │  └─ /app/skills/ (optional) │ │
│  └─────────┬───────────┘    └─────────────────────────────┘ │
│            │ gRPC                       │ HTTPS              │
│  ┌─────────▼───────────┐    ┌──────────▼──────────┐        │
│  │  Temporal Server    │    │  LLM Provider       │        │
│  │  durable execution  │    │  OpenAI / Vertex    │        │
│  │  + state            │    │  OpenAI agents SDK  │        │
│  └─────────────────────┘    └─────────────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

### Workflow Runner

The stateless orchestrator. A FastAPI app that embeds a Temporal worker. Receives workflow run requests via REST, starts Temporal workflow executions, and dispatches steps as Temporal activities to sandbox pods. Callers can supply their own `workflow_id` for idempotency; if omitted, a random ID is generated. Duplicate submissions with the same `workflow_id` return `409 Conflict`.

- **Temporal AgentWorkflow** — a single `@workflow.defn` class that interprets any workflow YAML at runtime. Handles conditions, retry, approval signals, and parallel groups. Registered once at worker startup — new workflow definitions don't require worker restarts.
- **Sandbox activities** — `run_sandbox_step` spawns an ephemeral container, calls `POST /v1/agent/run`, collects the result, and destroys the container. `send_approval_notification` dispatches approval requests to Slack/webhook/null notifiers. `build_escalation_activity` packages failed workflow context for human handoff.
- **DefinitionStore** — CRUD for workflow definitions with versioning. Definitions submitted via API. When initialized with a shared persistence backend, definitions are stored as JSON in the workflow state table (visible across all runner replicas); otherwise falls back to process-local in-memory storage. Runs bind to immutable snapshots.
- **Spawner** — `AgentSpawner` ABC with `KubernetesSpawner` and `PodmanSpawner` implementations. Handles `spawn()` → endpoint URL, `wait_ready()` → healthz polling, `destroy()` → cleanup, and `list_active()` → orphan detection.

### Sandbox Runtime (lightspeed-agentic-sandbox)

A single generic container image (`lightspeed-agentic-sandbox:latest`) that runs any agent. Agent identity comes from environment variables and the OpenAI agents SDK:

| Configuration | Purpose |
|---------------|---------|
| `LIGHTSPEED_PROVIDER` env var | LLM provider identifier (claude, openai, gemini) |
| `LIGHTSPEED_MODEL` env var | Model name or ID |
| Credential Secret (via `credentials_secret`) | K8s Secret or env var with API key |
| `/app/skills/` (optional) | Domain knowledge packages from skills OCI image |

The sandbox exposes a single endpoint — `POST /v1/agent/run` — accepting:
- `query` — the prompt for this step
- `context` — accumulated results from prior workflow steps
- `systemPrompt` — optional agent instructions
- `outputSchema` — optional structured output schema

### Temporal Server

Temporal Server provides durable execution and replaces the previous PostgreSQL persistence layer:

- **Workflow state** — step results, approval decisions, and event history are stored as workflow state within Temporal, not in an external database.
- **Retry and timeout** — `RetryPolicy` on each activity controls retry count; `start_to_close_timeout` enforces hard deadlines. No separate recovery poller needed.
- **Approval signals** — human approval is implemented as a Temporal signal (`AgentWorkflow.approve`), with `wait_condition` blocking until the signal arrives or times out.
- **Parallel execution** — steps sharing a `parallel_group` are dispatched via `asyncio.gather` within the workflow.
- **Crash recovery** — two mechanisms handle runner restarts:
  - **Content-hash pod naming** — `compute_pod_name()` derives deterministic pod names from `(workflow_id, step_name, attempt)`, making retries idempotent. If a retry spawns a pod with the same name as a previous attempt, the existing pod is reused or replaced cleanly.
  - **Startup orphan reconciliation** — `reconcile_orphaned_sandboxes()` runs at worker startup, scans for containers with the `spawned-by=workflow-runner` label, and destroys them. This cleans up any sandbox pods left behind by a crashed runner before Temporal re-dispatches their activities.

### Security

- **TLS for Temporal gRPC** — optional mutual TLS via `TEMPORAL_TLS_CERT_PATH`, `TEMPORAL_TLS_KEY_PATH`, `TEMPORAL_TLS_CA_PATH` environment variables
- **securityContext on pods** — advisory mode sets `read_only=True` on sandbox containers; scoped ServiceAccounts per step via `permissions.service_account`
- **K8s Secrets** — API keys injected via `credentials_secret` reference, never as plain env vars in pod specs
- **Explicit risk_level** — workflow steps declare risk level; missing risk_level fails closed to "high" (manual approval required)
- **Bearer auth** — workflow API endpoints protected by configurable auth dependency; fails closed when `AUTH_REQUIRED=true`
- **PermissionScope enforcement** — `allowed_tools`/`denied_tools` in request context filters tools at runtime
- **Audit trail** — `emit_audit()` logs sandbox spawn/destroy and escalation events with workflow and step correlation
- **Concurrency cap** — `MAX_SPAWNED_PODS` prevents resource exhaustion from runaway workflows

### Spawner

Abstract interface for creating and destroying agent pods on demand:

- **KubernetesSpawner** — creates K8s Jobs with scoped ServiceAccounts, resource limits via `SpawnConfig`, skills init containers, credential Secret mounts
- **PodmanSpawner** — creates Podman containers with env vars, port mapping, network configuration

Both implement `spawn()` → endpoint URL, `wait_ready()` → healthz polling, `destroy()` → cleanup, `list_active()` → orphan enumeration.

## Workflow Definition

```yaml
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: diagnose-and-fix
spec:
  steps:
    - name: diagnose
      type: agent
      agent: diagnostic-agent
      prompt: "Check all hosts for issues."
      output_key: diagnosis
      spawn: ephemeral

    - name: approve
      type: human-approval
      message: "Review diagnosis and approve remediation."
      output_key: approval

    - name: fix
      type: agent
      agent: diagnostic-agent
      prompt: "Fix issues found: {{ steps.diagnosis.output.summary }}"
      output_key: fix
      spawn: ephemeral
      condition: "steps.approval.output.approved == true"

    - name: verify
      type: agent
      agent: diagnostic-agent
      prompt: "Verify the cluster is healthy."
      output_key: verification
      spawn: ephemeral
```

## Agent Definition

The agent runtime also supports standalone agent definitions (not part of workflow execution):

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: diagnostic-agent
spec:
  instructions: |
    You are a cluster diagnostic agent...
  output_type: DiagnosticReport
  tools:
    module: diagnostic_tools
    functions: [list_hosts, check_host, run_remediation]
    read_only: [list_hosts, check_host]
  lifecycle:
    type: request-response
```

## Important Considerations

### Security

- **Ephemeral pods are untrusted** — they never receive database credentials or secrets beyond their API token
- **Step results flow through the trusted runner** — agents POST results to sandbox activities, not directly to any database
- **Tool filtering in advisory mode** — read-only classification removes write-capable tools when the workflow runs in advisory mode; sandbox filesystem set to read-only
- **Auth middleware** — configurable auth dependency on all workflow endpoints; fails closed when AUTH_REQUIRED=true
- **Approval RBAC** — designed for per-step approver scoping (backlog: channel plugins + identity integration)
- **MCP secret injection** — MCP servers can reference secrets via file-reference mounts. The `MCP_ALLOWED_SECRETS` environment variable defines an allowlist of permitted secret names; any secret not in the allowlist is rejected at activity dispatch time
- **TLS everywhere** — optional mutual TLS on Temporal gRPC; HTTPS between sandbox and LLM providers

### Structured Output

Every agent returns a Pydantic model (e.g., `DiagnosticReport`) with:
- `confidence` — how sure the agent is (low/medium/high)
- `risk_level` — risk of proposed actions
- `rollback_plan` — what to do if things go wrong
- `required_permissions` — what access is needed

This makes every response reviewable, comparable, and actionable.

### Retry with Context

Failed steps retry with full failure history. Each attempt sees what was tried before and why it failed. Temporal's `RetryPolicy` controls the retry count (`maximum_attempts`), and the activity timeout (`start_to_close_timeout`) enforces hard deadlines per attempt. After exhausting retries, the framework generates an **escalation handoff** — a complete document for human operators with all evidence collected, delivered via configurable escalation channels (log, webhook).

### Observability

- **OpenTelemetry** — distributed traces across workflow runner → Temporal → sandbox pods → LLM; Temporal `TracingInterceptor` propagates spans across workflow/activity boundaries
- **Prometheus** — per-run and per-tool metrics (`ls_agent_runs_total`, `ls_agent_tool_calls_total`); `/metrics` endpoint on the workflow runner
- **Structured logging** — JSON-formatted logs with workflow/step correlation
- **Correlation IDs** — validated, propagated across all requests
- **Health probes** — `/healthz`, `/livez`, `/readyz` (readyz returns 503 when Temporal is unreachable)

## Phase History

| Phase | Focus | Status |
|-------|-------|--------|
| 1a | Diagnostic agent in container, cross-pod HTTP | Done |
| 1b | Monitoring agent, async runs, observability | Done |
| 2 | Generic agent runtime template image | Done |
| 3 | Workflow executor with approval gates | Done |
| 4a | Auth middleware, enriched models, retry | Done |
| 4b | PostgreSQL persistence, on-demand spawning | Done |
| 4c | OTel tracing, metrics, SSE, advisory mode, MCP | Done |
| 5 | pydantic-graph exploration, ephemeral-by-default | Done |
| 6 | Stateless workflow runner, definition API, recovery poller | Done |
| 7 | Security hardening: K8s Secrets, explicit risk_level, bearer auth, derive_status, PermissionScope enforcement, FilePersistence CAS | Done |
| PoC2-1 | Temporal engine + sandbox activities | Done |
| PoC2-2 | Policy layer (auto-approve, advisory, permissions) | Done |
| PoC2-3 | Productization (Containerfile, OTel, Helm, CI, TLS) | Done |
