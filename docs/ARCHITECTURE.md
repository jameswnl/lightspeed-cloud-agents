# Cloud Agents Framework — Architecture

## Overview

The Cloud Agents Framework is an **agent and workflow orchestration platform**. It enables product teams to define, deploy, and operate AI agents and multi-step workflows as server-side services.

The framework uses **Temporal** for durable workflow execution and isolated runtime containers for step execution. Each workflow step runs in a spawned runtime behind an HTTP contract, which lets compatible runtime images participate in workflow execution without changing the orchestration engine.

## Goals & Objectives

1. **Bring your own agents & workflows** — define agents and multi-step agentic workflows via YAML + any tools. No forking, no rebuilds, no framework changes. Product teams deploy AI agents without changing framework code.

2. **Secured & governed execution** — each step runs in its own disposable container with scoped permissions, hard timeouts, and no shared state. Untrusted runtimes receive only the credentials explicitly configured for the step. Human oversight on high-risk operations via approval gates. Full observability: tracing, metrics, event streaming.

3. **Composable agent ecosystem** — agents and workflows are reusable building blocks. A chatbot invokes workflows as tools. Workflows chain agents. Multiple trigger points: conversations, alerts, API, schedules.

4. **Human follow-up with preserved context** — when automation reaches its limit, the framework can package workflow context for review, escalation, and operator follow-up.

5. **Kubernetes and Podman deployment targets** — the same orchestration model can run on both, with deployment-specific security and operational controls.

## Design Principles

### Framework, not pre-built agents

The diagnostic and monitoring agents are **examples**, not the product. The framework provides:
- Generic runtime and sandbox execution patterns
- Temporal workflow engine with conditions, retry, approval, parallel steps
- Spawner abstraction (K8s Jobs / Podman containers)
- Durable execution via Temporal
- Observability (tracing, metrics, events)

Product teams provide:
- `agent.yaml` — instructions, tools, output type, lifecycle
- Tool modules — Python functions the agent can call
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

All steps currently use ephemeral spawning. Pre-deployed (long-running) agents are a backlog item — the `spawn` field exists in the schema but is not yet implemented.

### Durable execution via Temporal

The workflow runner delegates workflow durability, retry, and signaling semantics to Temporal. This enables:
- **Horizontal scaling** — multiple worker replicas behind a Service/LB
- **Pod resilience** — any replica crashes, Temporal re-dispatches activities to healthy workers
- **Cross-replica operations** — start on replica A, approve on replica B via Temporal signals
- **Automatic retry** — Temporal handles step retry with configurable `RetryPolicy`
- **Timeout enforcement** — Temporal kills activities that exceed `start_to_close_timeout`

### Dual deployment: Kubernetes and Podman

Both deployment targets use the same workflow model but rely on different operational and security mechanisms:

| Capability | Kubernetes | Podman |
|-----------|-----------|--------|
| Ephemeral spawning | K8s Jobs + Services | Podman containers + port mapping |
| Networking | K8s Services + ClusterIP DNS | Podman network + container DNS |
| RBAC | ServiceAccounts + RoleBindings | OS-level access control |
| NetworkPolicy | Enforced by CNI | Host firewall rules |
| Durable execution | Temporal deployment | Temporal deployment |
| Config distribution | Env vars + K8s Secrets | Env vars |

The spawner abstraction (`AgentSpawner`) keeps workflow behavior consistent while allowing deployment-specific controls.

### Human-in-the-loop by design

The framework supports phased execution when workflows need explicit review points:

1. **Diagnose** — gather evidence, identify root cause
2. **Propose** — present options with risk levels and rollback plans
3. **Gate** — human reviews and approves (or auto-approve for low-risk)
4. **Execute** — carry out the approved plan
5. **Verify** — independently confirm the fix worked

Policy-driven approval can classify steps by risk and auto-approve selected categories.

Notification delivery is pluggable. Approval routing and identity-aware policy can evolve independently of the workflow engine.

## Architecture Components

```
┌─────────────────────────────────────────────────────────────┐
│  K8s Cluster / Podman Host                                  │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │  Platform Framework  │    │  Sandbox Pods (per step)    │ │
│  │                     │    │                             │ │
│  │  Workflow Runner    │───▶│  Sandbox Container          │ │
│  │  ├─ Temporal Worker │    │  ├─ Runtime HTTP contract   │ │
│  │  ├─ Spawner         │    │  ├─ Agent runtime + tools   │ │
│  │  └─ Definition Store│    │  └─ /app/skills/ (optional) │ │
│  └─────────┬───────────┘    └─────────────────────────────┘ │
│            │ gRPC                       │ HTTPS              │
│  ┌─────────▼───────────┐    ┌──────────▼──────────┐        │
│  │  Temporal Server    │    │  LLM Provider       │        │
│  │  durable execution  │    │  OpenAI / Vertex    │        │
│  │  + state            │    │  / other providers  │        │
│  └─────────────────────┘    └─────────────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

### Workflow Runner

The stateless orchestrator. A FastAPI app that embeds a Temporal worker. Receives workflow run requests via REST, starts Temporal workflow executions, and dispatches steps as Temporal activities to sandbox pods. Callers can supply their own `workflow_id` for idempotency; if omitted, a random ID is generated. Duplicate submissions with the same `workflow_id` return `409 Conflict`.

- **Temporal AgentWorkflow** — a single `@workflow.defn` class that interprets any workflow YAML at runtime. Handles conditions, retry, approval signals, and parallel groups. Registered once at worker startup — new workflow definitions don't require worker restarts.
- **Sandbox activities** — `run_sandbox_step` spawns an ephemeral container, calls the runtime HTTP interface, collects the result, and destroys the container. `send_approval_notification` dispatches approval requests to pluggable notifiers. `build_escalation_activity` packages failed workflow context for follow-up.
- **DefinitionStore** — CRUD for workflow definitions with versioning. The current app wiring uses an in-memory store. Shared persistence is an extension point rather than the default runtime behavior.
- **Spawner** — `AgentSpawner` ABC with `KubernetesSpawner` and `PodmanSpawner` implementations. Handles `spawn()` → endpoint URL, `wait_ready()` → readiness polling, `destroy()` → cleanup, and `list_active()` → orphan detection.

### Sandbox Runtime

A spawned runtime is an HTTP service that executes a step with agent-specific configuration supplied by the workflow engine. The current implementation passes provider and runtime configuration through environment variables and optional mounted content such as skills:

| Configuration | Purpose |
|---------------|---------|
| `LIGHTSPEED_PROVIDER` env var | LLM provider identifier (claude, openai, gemini) |
| `LIGHTSPEED_MODEL` env var | Model name or ID |
| Credential Secret (via `credentials_secret`) | K8s Secret or env var with API key |
| `/app/skills/` (optional) | Domain knowledge packages from skills OCI image |

The architecture should treat the runtime interface generically: the workflow engine sends a prompt plus workflow context and receives structured output. Exact route shapes and runtime adapters are implementation details that may change as the runtime contract is unified.

### Temporal Server

Temporal provides durable execution for workflow runs:

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
- **K8s Secrets** — provider and MCP credentials can be injected through explicit secret references
- **Explicit risk_level** — agent steps can declare risk level; missing `risk_level` defaults to manual approval behavior
- **Bearer auth** — workflow API endpoints protected by configurable auth dependency; fails closed when `AUTH_REQUIRED=true`
- **PermissionScope model** — `allowed_tools`/`denied_tools` defined per step; enforced in the generic agent runtime (not yet wired through the sandbox HTTP contract)
- **Audit trail** — `emit_audit()` logs sandbox spawn/destroy and escalation events with workflow and step correlation
- **Concurrency cap** — `MAX_SPAWNED_PODS` prevents resource exhaustion from runaway workflows

### Spawner

Abstract interface for creating and destroying agent pods on demand:

- **KubernetesSpawner** — creates K8s Jobs with scoped ServiceAccounts, resource limits via `SpawnConfig`, skills init containers, credential Secret mounts
- **PodmanSpawner** — creates Podman containers with env vars, port mapping, network configuration

Both implement `spawn()` → endpoint URL, `wait_ready()` → readiness polling, `destroy()` → cleanup, `list_active()` → orphan enumeration.

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
      prompt: "Check all hosts for issues."
      output_key: diagnosis
      output_schema:
        type: object
        properties:
          summary: { type: string }
          issues_found: { type: integer }
        required: [summary, issues_found]

    - name: approve
      type: human-approval
      message: "Review diagnosis and approve remediation."
      output_key: approval
      risk_level: high

    - name: fix
      type: agent
      prompt: "Fix issues found: {{ steps.diagnosis.output.summary }}"
      output_key: fix
      condition: "steps.approval.output.approved == true"
      timeout_seconds: 120

    - name: verify
      type: agent
      prompt: "Verify the cluster is healthy."
      output_key: verification
```

## Agent Definition

The runtime also supports standalone agent definitions (not part of workflow execution):

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

- **Ephemeral runtimes are untrusted** — they should receive only the credentials explicitly required for the step being executed
- **Step results flow through the runner** — the workflow engine controls spawning, request dispatch, and result collection
- **Advisory mode** — sandbox filesystem set to read-only; tool-level filtering is defined in the model but not yet enforced through the sandbox HTTP contract
- **Auth middleware** — configurable auth dependency on all workflow endpoints; fails closed when AUTH_REQUIRED=true
- **Approval RBAC** — approval policy and routing can be layered on top of workflow pause/resume semantics
- **MCP secret injection** — MCP servers can reference secrets via file-reference mounts. The `MCP_ALLOWED_SECRETS` environment variable defines an allowlist of permitted secret names; any secret not in the allowlist is rejected at activity dispatch time
- **TLS everywhere** — optional mutual TLS on Temporal gRPC; HTTPS between sandbox and LLM providers

### Structured Output

Agents return structured output, but the schema is agent-defined rather than framework-fixed. Built-in examples include models such as `DiagnosticReport` and `MonitoringResult`, and custom output types can be supplied through agent definitions.

### Retry with Context

Failed steps retry with full failure history. Each attempt sees what was tried before and why it failed. Temporal's `RetryPolicy` controls the retry count (`maximum_attempts`), and the activity timeout (`start_to_close_timeout`) enforces hard deadlines per attempt. After exhausting retries, the framework generates an **escalation handoff** — a complete document for human operators with all evidence collected, delivered via configurable escalation channels (log, webhook).

### Observability

- **OpenTelemetry** — distributed traces across workflow runner → Temporal → sandbox pods → LLM; Temporal `TracingInterceptor` propagates spans across workflow/activity boundaries
- **Prometheus** — per-run and per-tool metrics (`ls_agent_runs_total`, `ls_agent_tool_calls_total`); `/metrics` endpoint on the workflow runner
- **Structured logging** — JSON-formatted logs with workflow/step correlation
- **Correlation IDs** — validated, propagated across all requests
- **Health probes** — `/healthz`, `/livez`, `/readyz` (readyz returns 503 when Temporal is unreachable)
