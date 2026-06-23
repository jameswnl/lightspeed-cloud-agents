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

## Recommended Priority

Not all workstreams need to ship together. Suggested priority:

**P0 (must-have for production):**
1. Full API authentication (Workstream A)
2. Database-backed workflow persistence (Workstream B)
3. Wire into `/query` endpoint (Workstream C) — blocked on LCORE-2310
4. NetworkPolicy + ServiceAccount RBAC (Workstream A)

**P1 (high value):**
5. On-demand agent pod spawning (Workstream B)
6. Real cluster APIs (Workstream C)
7. OpenTelemetry tracing (Workstream B)
8. Approval via Slack/webhook (Workstream D)
9. Condition precedence fix (Workstream D) — known bug in shipped code

**P2 (can wait):**
10. Parallel step execution (Workstream D)
11. Retry policies (Workstream D)
12. Per-tool metrics (Workstream B)
13. MCP tools in agent.yaml (Workstream C)
14. AI-generated workflows (Workstream D)
15. Nested path interpolation (Workstream D)
16. SSE streaming for agent progress (Workstream B)

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
- **Agent spawning**: `KubernetesSpawner` vs `PodmanSpawner`
- **Deployment manifests**: K8s Deployments/Services vs Podman compose
- **Networking**: K8s Services/ClusterIP vs Podman shared network
- **RBAC**: K8s ServiceAccounts/RoleBindings vs no-op (Podman has no RBAC)
- **Persistence**: K8s PVCs for state directory vs local volume mounts
- **Health probes**: K8s readiness/liveness probes vs manual polling

Every new feature in P0-P2 must work on both targets. The test matrix:
- Unit tests: target-agnostic (no containers)
- E2E tests: run against both Kind (K8s) and Podman compose
- CI: two E2E pipelines (or parameterized with `DEPLOYMENT_TARGET`)

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
    # Uses podman-py SDK
    # Resource limits via --cpus and --memory flags
    # Max concurrent containers: same MAX_SPAWNED_PODS limit
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

---

## Effort Estimate

| Workstream | Items | Effort |
|-----------|-------|--------|
| A: Security & Auth | 4 items | 5-7 days |
| B: Production Infrastructure | 4 items | 8-10 days |
| C: LCS Integration | 3 items | 5-7 days |
| D: Workflow Enhancements | 6 items (P1+P2) | 7-10 days |
| **Total** | **17 items** | **25-34 days** |

Phase 4 is a full quarter of work (2-3 engineers, 10-12 weeks). The workstreams can be parallelized across engineers.

---

## TDD Task Breakdown (P0 items only)

| Task | What | Workstream | Est. |
|------|------|-----------|------|
| 1 | API auth middleware for agent endpoints | A | 2d |
| 2 | API auth middleware for workflow endpoints | A | 1d |
| 3 | NetworkPolicy manifests for Kind/OCP | A | 1d |
| 4 | ServiceAccount + RoleBinding manifests | A | 1d |
| 5 | PostgresPersistence implementation | B | 2d |
| 6 | PostgresPersistence integration tests | B | 1d |
| 7 | AgentSpawner interface + KubernetesSpawner | B | 3d |
| 8 | AgentSpawner integration with WorkflowExecutor | B | 2d |
| 9 | PodmanSpawner implementation | B | 1d |
| 10 | Wire RemoteAgentClient into build_agent() | C | 2d |
| 11 | E2E: authenticated agent calls across pods | A+B | 2d |

**P0 total: ~18 days, ~22 with reviews**

---

## Dependencies

| Dependency | Status | Blocks |
|-----------|--------|--------|
| LCORE-2310 (endpoint swap) | In Progress | Workstream C (wire into /query) |
| LCORE-2076 (skills wiring) | New | Workstream C (skills in production agents) |
| PostgreSQL in deployment | Infrastructure | Workstream B (database persistence) |
| K8s cluster access from pods | Infrastructure | Workstream B (on-demand spawning) |
