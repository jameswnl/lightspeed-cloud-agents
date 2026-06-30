# Phase 4: Production Readiness — Design

**Date**: 2026-06-23
**Prerequisite**: Phases 1-3 complete
**Focus**: Move from dev/test PoC to production-deployable cloud agents

---

## Problem

Phases 1-3 proved the architecture: generic agent runtime, multi-agent collaboration, workflow executor with approval gates. But everything runs in dev/test mode:

- No authentication on agent/workflow APIs
- No real cluster APIs (simulated state)
- No production persistence (in-memory or file-based)
- No on-demand pod spawning (pre-deployed only)
- No integration with lightspeed-stack's `/query` endpoint
- No OpenTelemetry distributed tracing

Phase 4 bridges the gap between "working PoC" and "deployable in customer clusters."

---

## Scope: What's in Phase 4

Phase 4 is organized into **four workstreams** that can be developed in parallel:

### Workstream A: Security & Auth

| Item | What | Origin |
|------|------|--------|
| Full API authentication | All agent/workflow endpoints behind auth (not just approval) | Phase 3 trust boundary |
| NetworkPolicy | Agent pods isolated by role (monitoring→diagnostic only) | Phase 1b security review |
| ServiceAccount RBAC | Per-agent K8s service accounts with scoped roles | Phase 1b security review |
| Tool signature verification | Validate mounted tool modules before loading | Phase 2 security review |

### Workstream B: Production Infrastructure

| Item | What | Origin |
|------|------|--------|
| Database-backed persistence | PostgreSQL for workflow state (replace file/memory) | Phase 3 deferred |
| On-demand agent pod spawning | K8s Jobs or Podman containers per workflow step | Phase 3 deferred |
| OpenTelemetry tracing | Distributed traces across agent pods and workflow steps | Phase 1b deferred |
| Per-tool Prometheus metrics | `ls_agent_tool_calls_total{agent_name, tool_name}` | Phase 1b deferred |

### Workstream C: Integration with Lightspeed-Stack

| Item | What | Origin |
|------|------|--------|
| Wire into `/query` endpoint | `build_agent()` uses `RemoteAgentClient` for delegation | LCORE-2310 dependency |
| MCP tool integration in agent.yaml | `type: mcp` tools in agent definitions | Phase 2 deferred |
| Real cluster APIs | Replace simulated state with K8s/OCP API calls | Phase 1b deferred |

### Workstream D: Workflow Enhancements

| Item | What | Origin |
|------|------|--------|
| Parallel step execution | Steps that don't depend on each other run concurrently | Phase 3 deferred |
| Retry policies per step | Configurable retry count and backoff per workflow step | Phase 3 deferred |
| Approval via Slack/email | Webhook-based notifications for approval steps | Phase 3 deferred |
| Nested path interpolation | `{{ steps.X.output.actions[0].host }}` | Phase 3 deferred |
| Condition precedence fix | Proper and/or parsing (currently broken for mixed) | Phase 3 retroactive review |
| AI-generated workflows | Workflow Designer Agent creates workflow YAML | Phase 2/3 deferred |

---

## What's NOT in Phase 4 (explicitly deferred to Phase 5 or dropped)

| Item | Decision | Rationale |
|------|----------|-----------|
| CRD-based K8s operator | Phase 5 | Needs operator-sdk, significant K8s expertise |
| Hot-reload of agent.yaml | Dropped | Pod restart is sufficient; adds complexity with little value |
| Dynamic output type registration (inline Pydantic schemas) | Dropped | `output_type_module` importlib approach covers the need |
| Tool dependency installation at runtime | Dropped | Derived images are the production pattern |
| Workflow visualization (graph rendering UI) | Phase 5 | Needs frontend work, not a backend concern |
| Workflow-to-workflow composition (nested workflows) | Phase 5 | Requires recursive executor design |
| Workflow versioning and rollback | Phase 5 | Requires schema migration + state compatibility |
| Output validator as YAML rule | Dropped | Python function validators are flexible enough |
| SSE streaming for agent progress | Phase 4 P2 (added below) | Useful but not blocking production deployment |
| Run state persistence for agent pods | Covered by database persistence | Workflow persistence covers this — agent runs are tracked in workflow state |
| Per-skill name filtering | Dropped | All skills in directory is sufficient; filtering adds complexity |

---

## Phased Delivery

Phase 4 is too large for a single delivery. Split into three sub-phases based on natural milestones:

### Phase 4a: Hardened PoC (~10 days)

**Goal:** Make it safe to demo outside dev/test. No external dependencies.

| # | Item | Workstream | Est. |
|---|------|-----------|------|
| 1 | Full API authentication (all endpoints) | A | 2d |
| 2 | NetworkPolicy + ServiceAccount RBAC | A | 2d |
| 3 | Condition precedence fix (shipped bug) | D | 1d |
| 4 | Enriched output models (confidence, risk, rollback plan, permissions) | D | 2d |
| 5 | Context-aware retry with escalation (failure history, hard cap, handoff doc) | D | 3d |

**Acceptance:** All APIs authenticated. RBAC enforced. Condition evaluator handles mixed `and`/`or`. Diagnostic output includes risk assessment and rollback plan. Failed workflows produce an escalation handoff. No external dependencies needed.

### Phase 4b: Production Infrastructure (~15 days)

**Goal:** Deployable in real customer clusters. Has external dependencies.

| # | Item | Workstream | Est. | Dependency |
|---|------|-----------|------|-----------|
| 6 | Database-backed persistence (PostgreSQL) | B | 3d | PostgreSQL in cluster |
| 7 | On-demand agent pod spawning (K8s Jobs + Podman) | B | 5d | K8s API access from pods |
| 8 | Wire into `/query` endpoint | C | 2d | LCORE-2310 |
| 9 | Real cluster APIs (replace simulated state) | C | 3d | OCP/K8s cluster access |
| 10 | Policy-driven auto-approve (risk classification) | D | 2d | — |

**Acceptance:** Workflows survive pod restart (PostgreSQL). Agent pods spawn on demand and are destroyed after use. `/query` delegates to cloud agents. Agents query real K8s APIs, not simulated dicts. Low-risk steps auto-approve.

### Phase 4c: Observability & Advanced Features (~24 days)

**Goal:** Polish, power-user features, advanced workflow capabilities. Items can be cherry-picked independently.

| # | Item | Workstream | Est. |
|---|------|-----------|------|
| 11 | OpenTelemetry distributed tracing | B | 3d |
| 12 | Approval via Slack/webhook | D | 2d |
| 13 | Per-tool Prometheus metrics | B | 1d |
| 14 | MCP tools in agent.yaml | C | 2d |
| 15 | SSE streaming for agent progress | B | 2d |
| 16 | Advisory/read-only mode (diagnosis-only workflows) | D | 1d |
| 17 | Escalation packaging (auto-package audit trail into support ticket) | D | 2d |
| 18 | Per-task permission scoping (sandbox-level RBAC) | A | 2d |
| 19 | Parallel step execution | D | 3d |
| 20 | Nested path interpolation (`{{ steps.X.output.actions[0].host }}`) | D | 1d |
| 21 | AI-generated workflows (Workflow Designer Agent) | D | 5d+ |

**Note:** Item 21 (AI-generated workflows) is large enough to be its own Phase 5 if the scope expands.

**Acceptance:** Full observability (traces, metrics, streaming). Slack approval notifications. MCP tools configurable in YAML. Advisory mode for read-only diagnosis. Parallel workflow steps. AI agent that designs workflows.

### Deployment target switch

All phases must support **both OCP/K8s and Podman** as deployment targets. A single configuration switch selects the target:

```yaml
# In lightspeed-stack.yaml or workflow runner config
deployment:
  target: kubernetes           # "kubernetes" or "podman"
  kubernetes:
    namespace: cloud-agents
    service_account: workflow-runner
    image_pull_policy: IfNotPresent
  podman:
    network: cloud-agents
    socket: /run/podman/podman.sock
```

Or via environment variable: `DEPLOYMENT_TARGET=kubernetes|podman`

This switch controls:
- **Agent spawning**: `KubernetesSpawner` (OCP/K8s) vs `PodmanSpawner` (Podman deployments)
- **Deployment manifests**: K8s Deployments/Services vs Podman compose
- **Networking**: K8s Services/ClusterIP vs Podman shared network
- **RBAC**: K8s ServiceAccounts/RoleBindings vs no-op (Podman has no RBAC)
- **Persistence**: K8s PVCs for state directory vs local volume mounts
- **Health probes**: K8s readiness/liveness probes vs manual polling

### Parity contract

The two targets provide **behavioral parity** (same features available), not **security parity** (same guarantees). Kubernetes provides hardening that Podman cannot match:

| Capability | Kubernetes | Podman |
|-----------|-----------|--------|
| On-demand spawning | Scoped ServiceAccount, K8s Jobs | Host-level socket access (secure socket appropriately) |
| NetworkPolicy | Enforced by CNI | No equivalent (use host firewall) |
| RBAC | ServiceAccount + RoleBinding | No equivalent (use OS-level access control) |
| Auth | TokenReview API | Shared secret / bearer token |
| Persistence | PVC-backed | Local volume |

**Both Kubernetes and Podman are supported production deployment targets.** Product teams (Ansible, RH Developer Hub) ship GA features on Podman. Kubernetes provides additional security hardening (NetworkPolicy, RBAC, ServiceAccount scoping) that Podman deployers should compensate for with host-level controls.

### Test matrix
- Unit tests: target-agnostic (no containers)
- E2E tests: run against both Kind (K8s) and Podman compose
- Security-specific tests (RBAC, NetworkPolicy): Kubernetes-only

---

## On-Demand Agent Spawning (Detail)

The most architecturally significant new capability. Instead of pre-deployed agent pods, the workflow executor creates pods on demand:

```yaml
# In workflow step
- name: diagnose
  type: agent
  agent: diagnostic-agent
  spawn: on-demand          # "pre-deployed" (default) or "on-demand"
  prompt: "Diagnose the cluster"
  output_key: diagnosis
```

### Implementation

```python
class AgentSpawner(ABC):
    """Abstract interface for spawning agent pods."""
    async def spawn(self, agent_name: str, agent_def: AgentDefinition) -> str:
        """Spawn an agent pod, return its endpoint URL."""
    async def destroy(self, endpoint: str) -> None:
        """Destroy a spawned agent pod."""
    async def wait_ready(self, endpoint: str, timeout: float = 60) -> bool:
        """Wait for the agent pod to be ready."""

class KubernetesSpawner(AgentSpawner):
    """Spawns K8s Jobs for on-demand agents."""
    # Uses kubernetes Python client (BatchV1Api)
    # ttlSecondsAfterFinished for auto-cleanup
    # Resource limits from AgentDefinition.spec.resources (cpu, memory)
    # Max concurrent pods: configurable via MAX_SPAWNED_PODS env var (default: 10)

class PodmanSpawner(AgentSpawner):
    """Spawns Podman containers for on-demand agents."""
    # Uses podman-py SDK via Podman socket
    # Resource limits via --cpus and --memory flags
    # Max concurrent containers: same MAX_SPAWNED_PODS limit
    #
    # SECURITY NOTE: Podman spawning requires Podman socket access,
    # which grants host-level container control. This is NOT equivalent
    # to Kubernetes Jobs with scoped ServiceAccounts. Deployers should
    # secure the Podman socket and restrict access to authorized services.
```

### Safety controls

- **Resource limits**: Spawned pods inherit `resources.cpu` and `resources.memory` from the `AgentDefinition` spec. No unbounded pods.
- **Concurrency cap**: `MAX_SPAWNED_PODS` env var (default: 10). If the cap is reached, the spawner blocks until a pod completes. Prevents pod storms from parallel workflows or runaway loops.
- **Auto-cleanup**: K8s Jobs use `ttlSecondsAfterFinished=300`. Podman containers are removed in the `finally` block. Orphaned pods are cleaned up on workflow runner restart.
- **Timeout**: Spawned pods must pass `/healthz` within 60s or the step fails.

### Workflow executor integration

```python
# In WorkflowExecutor._execute_agent_step():
if step.spawn == "on-demand":
    endpoint = await self._spawner.spawn(step.agent, agent_def)
    try:
        await self._spawner.wait_ready(endpoint)
        client = RemoteAgentClient(endpoint)
        response = await client.run(prompt)
    finally:
        await self._spawner.destroy(endpoint)
else:
    client = self._client_factory(step.agent)
    response = await client.run(prompt)
```

---

## Database-Backed Persistence (Detail)

Replace `InMemoryPersistence` / `FilePersistence` with PostgreSQL:

```python
class PostgresPersistence(WorkflowPersistence):
    """PostgreSQL-backed workflow state persistence."""

    def __init__(self, connection_string: str):
        self._engine = create_async_engine(connection_string)

    async def save(self, state: WorkflowState) -> None:
        """Upsert workflow state as JSON."""

    async def load(self, workflow_id: str) -> Optional[WorkflowState]:
        """Load workflow state by ID."""
```

Schema:
```sql
CREATE TABLE workflow_states (
    workflow_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
```

Alternatively, use DBOS for automatic checkpointing (wraps the executor).

---

## Full API Authentication (Detail)

Extend the shared-secret pattern from Phase 3 approval to all endpoints:

```python
# All agent/workflow endpoints require:
# Authorization: Bearer <token>
# Token from AGENT_API_TOKEN env var

# Or integrate with lightspeed-stack's existing auth modules:
# - k8s ServiceAccount token
# - JWK validation
# - noop (dev/test)
```

For Phase 4, reuse lightspeed-stack's existing `authentication` module pattern from `src/authentication/`. This avoids inventing a new auth system.

### Token propagation for spawned pods

When the workflow executor spawns an on-demand agent pod:
1. The workflow runner pod has its own K8s ServiceAccount with permission to create Jobs (`batch/v1` verbs: create, get, delete)
2. The spawned agent pod gets a dedicated ServiceAccount with scoped RBAC (e.g., `view` for diagnostic, `edit` for remediation)
3. Agent-to-agent auth: the workflow runner passes its bearer token in `RemoteAgentClient` requests. Spawned agents validate incoming tokens against the same shared secret or K8s TokenReview API.
4. LLM API keys: injected via K8s Secrets mounted as env vars (same pattern as Phase 1b)

### Default auth provider

- **Kind/dev**: shared-secret bearer token (`AGENT_API_TOKEN` env var) — same as Phase 3 approval auth, extended to all endpoints
- **OCP/production**: K8s ServiceAccount token validated via TokenReview API, integrated with LCS's existing `k8s` auth module

### Authorization model by sub-phase

Phase 4 introduces authentication progressively. Authorization (what an authenticated caller may do) starts coarse and narrows over time:

| Sub-phase | Authentication | Authorization | Notes |
|-----------|---------------|---------------|-------|
| **4a** | Bearer token on all endpoints | **Coarse** — any valid token grants full access to all agent/workflow APIs | Temporary. Acceptable because 4a targets staging, not production. |
| **4b** | Same + token propagation to spawned pods | **Inherited** — spawned agents inherit the workflow runner's identity. No per-agent audience scoping. | The workflow runner is trusted to call any agent it spawns. |
| **4c** | Same | **Fine-grained** — per-task permission scoping (item 18). Spawned pods get only the permissions approved for their specific task. | Full authorization model. |

This progression is intentional: 4a/4b get authentication right (who are you?), 4c adds authorization (what may you do?). The coarse model in 4a/4b is explicitly temporary — documented here so it's a conscious decision, not an oversight.

---

## Effort Estimate

| Sub-phase | Items | Effort | Dependencies |
|-----------|-------|--------|-------------|
| **4a: Hardened PoC** | 5 | ~10 days | None |
| **4b: Production Infrastructure** | 5 | ~15 days | LCORE-2310, PostgreSQL, K8s API access |
| **4c: Observability & Advanced** | 11 | ~24 days | None (cherry-pickable) |
| **Total** | **21** | **~49 days** | |

Phase 4a is a single sprint. Phase 4b is 2-3 sprints (blocked by dependencies). Phase 4c spans a quarter and items can be picked independently.

---

## TDD Task Breakdown

### Phase 4a tasks

| Task | What | Est. |
|------|------|------|
| 4a-1 | Auth middleware for agent runtime endpoints | 1d |
| 4a-2 | Auth middleware for workflow API endpoints | 1d |
| 4a-3 | NetworkPolicy manifests (Kind + OCP) | 1d |
| 4a-4 | ServiceAccount + RoleBinding manifests | 1d |
| 4a-5 | Fix condition evaluator and/or precedence | 1d |
| 4a-6 | Enriched DiagnosticReport (confidence, risk, rollback, permissions) | 2d |
| 4a-7 | Retry-with-context in WorkflowExecutor (failure history, hard cap) | 2d |
| 4a-8 | Escalation handoff document on retry exhaustion | 1d |
| 4a-9 | E2E: authenticated calls + retry + enriched output | 2d |

**Phase 4a total: ~12 days with reviews**

### Phase 4b tasks

| Task | What | Est. | Dep. |
|------|------|------|------|
| 4b-1 | PostgresPersistence implementation | 2d | PostgreSQL |
| 4b-2 | PostgresPersistence integration tests | 1d | PostgreSQL |
| 4b-3 | AgentSpawner ABC + KubernetesSpawner | 3d | K8s API |
| 4b-4 | PodmanSpawner | 1d | — |
| 4b-5 | Spawner integration with WorkflowExecutor | 2d | — |
| 4b-6 | Wire RemoteAgentClient into build_agent() | 2d | LCORE-2310 |
| 4b-7 | Replace simulated state with real K8s API tools | 3d | OCP cluster |
| 4b-8 | Policy-driven auto-approve (risk classification) | 2d | — |
| 4b-9 | E2E: on-demand spawning + real APIs + persistence | 2d | All above |

**Phase 4b total: ~20 days with reviews**

### Phase 4c tasks (cherry-pickable)

Individual items estimated in the phase table above. No fixed ordering — pick based on user demand.

---

## Dependencies

| Dependency | Status | Blocks |
|-----------|--------|--------|
| LCORE-2310 (endpoint swap) | In Progress | Workstream C (wire into /query) |
| LCORE-2076 (skills wiring) | New | Workstream C (skills in production agents) |
| PostgreSQL in deployment | Infrastructure | Workstream B (database persistence) |
| K8s cluster access from pods | Infrastructure | Workstream B (on-demand spawning) |
