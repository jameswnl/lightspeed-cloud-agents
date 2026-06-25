# Cloud Agents Framework — Architecture

## Overview

The Cloud Agents Framework is an **agent/workflow orchestration platform** built into lightspeed-stack. It enables product teams to create, deploy, and manage AI agents and multi-step workflows as server-side services in customer clusters.

The framework is built on **Pydantic AI** for agent execution and provides a generic runtime that runs any agent from declarative YAML configuration — no framework code changes required.

## Goals & Objectives

1. **Bring your own agents** — product teams define agents via YAML + Python tools. The framework runs them. No forking, no image rebuilds, no PRs to the platform repo.

2. **Multi-step workflows with human oversight** — chain agents into workflows with conditions, retry, approval gates, and escalation. Humans stay in the loop for high-risk operations.

3. **Ephemeral isolated execution** — each workflow step runs in its own disposable container. Clean state, scoped permissions, hard timeouts. A stuck or misbehaving agent can't affect other steps or the platform.

4. **Dual deployment targets** — the same agents and workflows run on both Kubernetes (OCP) and Podman. Product teams like Ansible and RH Developer Hub ship GA features on Podman. Both are first-class production targets.

5. **Stateless horizontal scaling** — the workflow runner scales behind a load balancer. All state lives in PostgreSQL. Any replica can serve any request. Pod crashes don't lose workflows.

6. **Production-grade observability** — OpenTelemetry distributed tracing, per-tool Prometheus metrics, SSE streaming for real-time progress, structured logging with correlation IDs.

## Design Principles

### Framework, not pre-built agents

The diagnostic and monitoring agents are **examples**, not the product. The framework provides:
- Generic agent runtime (one image for all agent types)
- Workflow executor with conditions, retry, approval, parallel steps
- Spawner abstraction (K8s Jobs / Podman containers)
- Persistence (PostgreSQL / in-memory)
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

### Stateless runner, durable state

The workflow runner holds no state in memory. All workflow state, step results, and definitions live in PostgreSQL. This enables:
- **Horizontal scaling** — multiple runner replicas behind a Service/LB
- **Pod resilience** — any replica crashes, others continue
- **Cross-replica operations** — start on replica A, approve on replica B
- **Recovery** — orphaned steps detected and retried by the recovery poller

Optimistic locking (version-based CAS) prevents duplicate advancement when multiple replicas race.

### Dual deployment: Kubernetes and Podman

Both deployment targets provide **behavioral parity** (same features) but different **security mechanisms**:

| Capability | Kubernetes | Podman |
|-----------|-----------|--------|
| Ephemeral spawning | K8s Jobs + Services | Podman containers + port mapping |
| Networking | K8s Services + ClusterIP DNS | Podman network + container DNS |
| RBAC | ServiceAccounts + RoleBindings | OS-level access control |
| NetworkPolicy | Enforced by CNI | Host firewall rules |
| Persistence | PVC-backed PostgreSQL | Local volume PostgreSQL |
| Config distribution | ConfigMaps | Volume mounts |

Deployers compensate for Podman's lack of K8s-native security with host-level controls. The spawner abstraction (`AgentSpawner` ABC) hides the deployment target from the workflow executor.

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
│  │  Platform Framework  │    │  Ephemeral Pods (per step)  │ │
│  │                     │    │                             │ │
│  │  Workflow Runner    │───▶│  agent-runtime:latest       │ │
│  │  ├─ Executor        │    │  ├─ Pydantic AI Agent       │ │
│  │  ├─ Spawner         │    │  ├─ /app/agent.yaml         │ │
│  │  ├─ Registry        │    │  ├─ /app/tools/*.py         │ │
│  │  ├─ Persistence ────┤    │  ├─ /app/skills/            │ │
│  │  └─ Recovery Poller │    │  └─ /app/registry.yaml      │ │
│  │                     │    │                             │ │
│  │  Agent Runtime      │    │  (spawned per step,         │ │
│  │  (generic image)    │    │   destroyed after)          │ │
│  └─────────────────────┘    └─────────────────────────────┘ │
│           │                            │                    │
│           ▼                            ▼                    │
│  ┌─────────────┐              ┌──────────────┐              │
│  │ PostgreSQL  │              │ LLM (OpenAI) │              │
│  │ ├─ Workflow │              │ pydantic-ai  │              │
│  │ │  state    │              │ tool calling │              │
│  │ ├─ Defs     │              └──────────────┘              │
│  │ └─ CAS lock │                                            │
│  └─────────────┘                                            │
└─────────────────────────────────────────────────────────────┘
```

### Workflow Runner

The stateless orchestrator. Receives workflow run requests, dispatches steps to ephemeral pods, handles approval gates, persists state.

- **WorkflowExecutor** — core execution loop with conditions, retry, approval, events
- **StepDispatcher** — spawns ephemeral pods and manages their lifecycle
- **RecoveryPoller** — background task detecting orphaned steps
- **DefinitionStore** — CRUD for workflow definitions with versioning
- **Persistence** — PostgreSQL-backed state with optimistic locking

### Agent Runtime

A single generic container image (`agent-runtime:latest`) that runs any agent. Agent identity comes entirely from mounted configuration:

| Mount | Purpose |
|-------|---------|
| `/app/agent.yaml` | Agent definition (instructions, tools, output type, lifecycle) |
| `/app/tools/*.py` | Python tool modules |
| `/app/skills/` | Domain knowledge packages (SKILL.md files) |
| `/app/registry.yaml` | Agent endpoint registry |

### Stateless Execution (Phase 6-7)

The workflow runner is stateless — all state lives in PostgreSQL:

- **DefinitionStore** — CRUD for workflow definitions with versioning. Definitions submitted via API, stored in shared persistence. Runs bind to immutable snapshots.
- **StepDispatcher** — dispatches workflow steps to ephemeral pods. Writes `"dispatched"` status + `spawned_name` to DB before spawning for crash recovery.
- **RecoveryPoller** — background task on every runner replica. Detects orphaned dispatched steps (past timeout), marks them failed, and calls `spawner.destroy()` to clean up the backing Job.
- **Optimistic locking** — `save_cas()` with version-based compare-and-swap prevents duplicate advancement across replicas.
- **derive_status()** — pure function that computes workflow status from step results, preventing status drift.

### Security (Phase 7)

- **K8s Secrets** — API keys injected via `secretKeyRef`, never as plain env vars in pod specs
- **Explicit risk_level** — workflow steps declare risk level; missing risk_level fails closed to "high" (manual approval required)
- **Bearer auth** — `RemoteAgentClient` sends `Authorization: Bearer` header; agent runtime validates via `BearerAuthMiddleware`
- **PermissionScope enforcement** — `allowed_tools`/`denied_tools` in request context actually filters tools at runtime in `generic_runner`
- **definition_snapshot** — every workflow run stores an immutable copy of its definition at creation time

### Spawner

Abstract interface for creating and destroying agent pods on demand:

- **KubernetesSpawner** — creates K8s Jobs with scoped ServiceAccounts, ConfigMap volume mounts, resource limits via SpawnConfig
- **PodmanSpawner** — creates Podman containers with volume mounts, port mapping, network configuration

Both implement `spawn()` → endpoint URL, `wait_ready()` → healthz polling, `destroy()` → cleanup.

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
- **Step results flow through the trusted runner** — agents POST results to a runner callback endpoint, not directly to the database
- **Tool filtering in advisory mode** — read-only classification removes write-capable tools when the workflow runs in advisory mode
- **Auth middleware** — BearerAuthMiddleware on all agent and workflow endpoints
- **Approval RBAC** — designed for per-step approver scoping (Phase 6 backlog: channel plugins + identity integration)

### Structured Output

Every agent returns a Pydantic model (e.g., `DiagnosticReport`) with:
- `confidence` — how sure the agent is (low/medium/high)
- `risk_level` — risk of proposed actions
- `rollback_plan` — what to do if things go wrong
- `required_permissions` — what access is needed

This makes every response reviewable, comparable, and actionable.

### Retry with Context

Failed steps retry with full failure history. Each attempt sees what was tried before and why it failed. After exhausting retries, the framework generates an **escalation handoff** — a complete document for human operators with all evidence collected.

### Observability

- **OpenTelemetry** — distributed traces across workflow runner → agent pods → LLM
- **Prometheus** — per-run and per-tool metrics (`ls_agent_runs_total`, `ls_agent_tool_calls_total`)
- **SSE streaming** — real-time workflow progress events
- **Correlation IDs** — validated, propagated across all requests

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
