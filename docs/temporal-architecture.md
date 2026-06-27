# Cloud Agents Framework — Temporal + Pydantic AI Architecture

**Date**: 2026-06-24
**Status**: Design proposal (rev 2 — addresses independent review feedback)

## Motivation

The Cloud Agents framework (Phase 7 complete) has a hand-rolled workflow executor with custom persistence, recovery polling, optimistic locking, and state management. These are solved problems in the workflow engine space.

**Pydantic AI has a native, first-party Temporal integration** (`pydantic-ai[temporal]`, stable since v2.0.0, co-maintained by Pydantic and Temporal teams) that wraps agents as durable Temporal activities with automatic crash recovery, retry, and state persistence.

This document proposes replacing the custom workflow executor with Temporal while keeping the existing Pydantic AI agents, spawner abstraction, and YAML-driven configuration.

### Why not keep the hand-rolled executor?

The current executor works. Phase 7 addressed the critical security and robustness gaps. But the hand-rolled code carries ongoing maintenance cost:

- `executor.py` (464 lines) + `advancement.py` (227 lines) + `persistence.py` (130 lines) + `step_dispatcher.py` (214 lines) + `postgres_persistence.py` (148 lines) = **1,183 lines** of orchestration code that reimplements what Temporal provides natively
- The graph executor path adds another **701 lines** (`graph_executor.py`, `graph_state.py`, `graph_steps.py`, `graph_builder_factory.py`, `parallel.py`) — a parallel implementation that must be maintained alongside the primary executor
- Every crash recovery edge case, CAS race condition, and orphaned-pod scenario is a custom bug to find and fix

Temporal eliminates these entire categories of bugs by design.

## What Temporal Replaces

| Current Component | Lines | Temporal Replacement |
|---|---|---|
| `WorkflowExecutor` | 464 | Temporal `@workflow.defn` class |
| `StepDispatcher` | 214 | `workflow.execute_activity()` |
| `RecoveryPoller` + CAS locking | 227 | Temporal's built-in timeout detection + single-threaded execution |
| `WorkflowPersistence` (all backends) | 278 | Temporal Server persistence |
| `GraphExecutor` + graph_* modules | 701 | Temporal workflow (see "Fate of the Graph Executor" below) |
| `WorkflowState.derive_status()` | 92 | Temporal event history + `@workflow.query` |
| Approval pause/resume logic | ~80 | Temporal signals + `workflow.wait_condition()` |
| **Total deletable** | **~2,056** | |

### What Temporal Does NOT Replace

| Component | Reason |
|---|---|
| `AgentSpawner` (K8s/Podman) | Ephemeral container spawning is a domain concern, runs as a Temporal activity |
| `create_generic_runner()` | Pydantic AI agent construction from YAML is unchanged |
| `AgentDefinition` / `WorkflowDefinition` YAML | Configuration layer is independent of the executor |
| `RemoteAgentClient` | HTTP calls to spawned agent pods unchanged |
| `AdvisoryEnforcer` / `PermissionScope` | Agent-level concerns, not workflow-level |
| `conditions.py` | Pure logic, reused in the Temporal workflow |
| `auto_approve.py` | Risk classification + policy, reused in the Temporal workflow |
| `retry.py` (escalation generation only) | `build_escalation()` is pure logic, reused |
| OpenTelemetry tracing | Coexists with Temporal's tracing |
| BearerAuthMiddleware | Auth on agent endpoints is independent |

## Fate of the Graph Executor

The codebase has two executor implementations:

1. **`WorkflowExecutor`** (464 lines) — production executor, sequential steps
2. **`GraphExecutor`** (269 lines) + supporting modules (432 lines) — experimental pydantic-graph executor with parallel step support

**Decision: Both are replaced by a single Temporal workflow class.**

- Sequential steps: Temporal workflow iterates steps in order (same as `WorkflowExecutor`)
- Parallel steps: Temporal supports `asyncio.gather()` on multiple `workflow.execute_activity()` calls within the workflow. The existing `parallel.py` validation logic (`group_steps()`, `validate_parallel_groups()`) moves into the Temporal workflow class
- The pydantic-graph dependency is removed — Temporal is the graph execution engine

This eliminates the "two executor" maintenance burden and unifies parallel + sequential execution in one place.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Customer Cluster (OCP or Podman)                                │
│                                                                  │
│  ┌──────────────────────┐     ┌────────────────────────────────┐ │
│  │  FastAPI Service      │     │  Temporal Server               │ │
│  │  (API + Temporal      │────▶│  K8s: Helm chart or Deployment │ │
│  │   Client)             │     │  Podman: podman-compose        │ │
│  │                       │     │  Port 7233 (gRPC)              │ │
│  │  POST /workflows/run  │     │  Port 8080 (Web UI)            │ │
│  │  POST /workflows/:id/ │     └──────────┬───────────────────┘ │
│  │       approve         │                │                      │
│  │  GET  /workflows/:id/ │                │ task dispatch        │
│  │       events (SSE)    │                ▼                      │
│  └──────────────────────┘     ┌────────────────────────────────┐ │
│                               │  Temporal Workers               │ │
│                               │  (horizontally scalable)        │ │
│                               │                                  │ │
│                               │  AgentWorkflow (generic)        │ │
│                               │  ├─ sequential / parallel steps │ │
│                               │  ├─ condition evaluation        │ │
│                               │  ├─ approval signals + wait     │ │
│                               │  └─ retry + escalation          │ │
│                               │                                  │ │
│                               │  Activities:                     │ │
│                               │  ├─ run_agent_in_process()       │ │
│                               │  ├─ run_agent_spawned()          │ │
│                               │  └─ send_notification()          │ │
│                               └──────────┬───────────────────┘ │
│                                          │                      │
│                               ┌──────────▼───────────────────┐ │
│                               │  Ephemeral Agent Pods         │ │
│                               │  (spawned steps only)         │ │
│                               └───────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Core Design

### Single Generic Workflow Class (YAML-Interpreted)

A single `AgentWorkflow` class interprets any YAML workflow definition at runtime. Workflow classes are NOT generated dynamically — one class handles all workflow definitions. The YAML is passed as the workflow input.

This preserves the `DefinitionStore` pattern: new workflow definitions can be submitted via API and executed immediately without worker restarts. The Temporal workflow class is registered once at worker startup and interprets whatever YAML it receives.

```python
@workflow.defn
class AgentWorkflow:
    def __init__(self):
        self._steps: dict[str, StepResult] = {}
        self._approval_decisions: dict[str, str] = {}
        self._events: list[WorkflowEvent] = []

    @workflow.signal
    async def approve(self, step_name: str, decision: str):
        self._approval_decisions[step_name] = decision

    @workflow.query
    def get_status(self) -> WorkflowStatus:
        return WorkflowStatus(steps=self._steps, events=self._events)

    @workflow.run
    async def run(self, input: WorkflowInput) -> WorkflowOutput:
        definition = input.definition
        grouped = group_steps_by_parallel(definition.steps)

        for group in grouped:
            if len(group) == 1:
                result = await self._execute_step(group[0], input)
                if result and result.status == "failed":
                    break
            else:
                # Parallel group
                results = await asyncio.gather(*[
                    self._execute_step(step, input)
                    for step in group
                ])
                if any(r.status == "failed" for r in results if r):
                    break

        return WorkflowOutput(steps=self._steps)

    async def _execute_step(self, step, input) -> StepResult | None:
        if step.condition and not evaluate_condition(step.condition, self._steps):
            self._steps[step.output_key] = StepResult(status="skipped")
            self._emit("step.skipped", step.name)
            return None

        if step.type == "human-approval":
            return await self._handle_approval(step, input.policy)

        # Agent step — dispatch to activity with Temporal-managed retries.
        #
        # Retry model: the activity RAISES exceptions for retriable failures
        # (infra errors, timeouts, transient LLM failures). Temporal retries
        # the activity automatically per RetryPolicy. The activity returns
        # StepResult(status="failed") only for non-retriable application
        # failures (e.g., agent completed but reported failure in output).
        #
        # After all Temporal retries are exhausted, execute_activity raises
        # ActivityError. The workflow catches it and triggers escalation.
        self._emit("step.started", step.name)
        try:
            result = await workflow.execute_activity(
                run_agent_step,
                args=[AgentStepInput(step=step, context=self._steps, config=input.config)],
                start_to_close_timeout=timedelta(seconds=step.timeout_seconds or 600),
                retry_policy=RetryPolicy(maximum_attempts=step.max_retries + 1),
            )
        except ActivityError:
            # All Temporal retries exhausted — escalate
            result = StepResult(status="failed", error="retries exhausted")
            self._steps[step.output_key] = result
            self._emit("step.failed", step.name)
            escalation = await workflow.execute_activity(
                build_escalation_activity,
                args=[self._steps],
                start_to_close_timeout=timedelta(seconds=60),
            )
            self._steps["escalation"] = escalation
            return result

        self._steps[step.output_key] = result
        self._emit("step.completed" if result.status == "completed" else "step.failed", step.name)
        return result

    def _emit(self, event_type: str, step_name: str):
        self._events.append(WorkflowEvent(
            type=event_type, step=step_name,
            timestamp=workflow.now().isoformat(),
        ))
```

### Agent Execution: Spawned Pods Only

**Decision: Spawned pods are the default and recommended execution path. All workflow steps use the spawner abstraction.**

The reviewer correctly identified that dual execution modes (in-process vs spawned) create confusion:
- Two different retry/recovery semantics
- Two different observability paths
- Non-obvious choice per step

The spawned-pod path is the production path. It provides container isolation, RBAC scoping, and consistent behavior across all steps. The `run_agent_step` activity handles the spawn → call → destroy lifecycle:

```python
@activity.defn
async def run_agent_step(input: AgentStepInput) -> StepResult:
    step = input.step
    spawner = get_spawner()  # K8s or Podman based on deployment

    # Content-hash name for idempotent retry
    pod_name = compute_spawn_name(
        input.config.workflow_id, step.name, activity.info().attempt
    )

    endpoint = await spawner.spawn(
        pod_name, image="agent-runtime:latest",
        env=build_env(step, input.config),
        secret_env_vars=input.config.secret_refs,
        config=step.spawn_config,
    )
    try:
        await spawner.wait_ready(endpoint)
        client = RemoteAgentClient(endpoint, auth_token=get_api_token())
        prompt = interpolate(step.prompt, input.context)
        response = await client.run(prompt=prompt)

        # Application-level failure (agent ran but reported failure).
        # This is non-retriable — returning StepResult tells Temporal
        # the activity succeeded (it did), but the workflow sees the
        # failed status and can decide what to do.
        if not response.success:
            return StepResult(status="failed", error=response.error or "agent reported failure")

        return StepResult(status="completed", output=response.output)

    # Infrastructure failures (spawn failed, HTTP timeout, pod crash)
    # are NOT caught — they propagate as exceptions that Temporal retries.
    finally:
        await spawner.destroy(pod_name)
```

**Pre-deployed agents**: For latency-sensitive steps where container startup cost is unacceptable (GOALS.md G3), the activity calls a persistent service endpoint instead of spawning a new pod. The spawner is simply not invoked — `RemoteAgentClient` calls the pre-registered endpoint from the agent registry directly. This is a configuration choice per step (`spawn: pre-deployed` vs `spawn: ephemeral`), not a different execution mode.

### Content-Hash Naming for Idempotent Spawning

Pod names include `workflow_id + step_name + attempt_number` in the hash, preventing collisions between different workflows using the same agent:

```python
def compute_spawn_name(workflow_id: str, step_name: str, attempt: int) -> str:
    content = f"{workflow_id}:{step_name}:{attempt}"
    hash_suffix = hashlib.sha256(content.encode()).hexdigest()[:8]
    return f"agent-{step_name}-{hash_suffix}"
```

This also enables the spawner to discover orphaned pods by name — if the activity retries, it can detect that a pod with the expected name already exists.

### Human Approval via Signals

```python
# FastAPI endpoint sends signal to workflow
@app.post("/workflows/{id}/approve")
async def approve_workflow(id: str, body: ApprovalRequest):
    handle = temporal_client.get_workflow_handle(id)
    await handle.signal(AgentWorkflow.approve, body.step_name, body.decision)
    return {"status": "signal_sent"}
```

The workflow durably waits — survives worker restarts, cluster migrations, and arbitrarily long human review periods:

```python
async def _handle_approval(self, step, policy):
    if auto_approve(step, policy):
        self._steps[step.output_key] = StepResult(status="completed", output={"approved": True})
        return self._steps[step.output_key]

    self._emit("workflow.paused", step.name)
    await workflow.wait_condition(
        lambda: step.name in self._approval_decisions,
        timeout=timedelta(seconds=step.timeout_seconds or 86400),
    )

    decision = self._approval_decisions.get(step.name, "denied")
    self._steps[step.output_key] = StepResult(
        status="completed" if decision == "approved" else "denied",
        output={"approved": decision == "approved"},
    )
    return self._steps[step.output_key]
```

### SSE: Accept Query Polling

**Decision: Use Temporal query polling at 1-second intervals. Do not add Redis.**

The reviewer correctly identified that a Redis pub/sub bridge adds complexity that negates Temporal's simplification benefit. The trade-off:

- **Current**: Near-zero latency SSE via `asyncio.Queue` callback
- **Temporal**: Up to 1 second latency via query polling

This is acceptable because:
1. Workflow steps take seconds to minutes (LLM calls, container spawning). 1-second polling granularity is imperceptible relative to step duration.
2. The events list stored in the workflow (`self._events`) provides full event history, not just latest state. Clients that reconnect get the complete timeline.
3. Temporal queries are lightweight gRPC calls — the load on the server is minimal for the expected concurrency (<100 active workflows per cluster).

If sub-second event delivery becomes a hard requirement for a future use case, the `event_stream_handler` → external message queue path is available as an opt-in enhancement, not a core architectural component.

```python
@app.get("/workflows/{id}/events")
async def stream_events(id: str):
    async def generator():
        handle = temporal_client.get_workflow_handle(id)
        last_seen = 0
        while True:
            status = await handle.query(AgentWorkflow.get_status)
            new_events = status.events[last_seen:]
            for event in new_events:
                yield {"event": event.type, "data": event.json()}
            last_seen = len(status.events)
            if any(e.type in ("workflow.completed", "workflow.failed") for e in new_events):
                break
            await asyncio.sleep(1)
    return EventSourceResponse(generator())
```

## Deployment

### Kubernetes (OCP)

| Component | How |
|---|---|
| Temporal Server | Helm chart (`temporalio/temporal`) or Deployment |
| PostgreSQL | Managed (RDS/CloudSQL) or StatefulSet |
| Temporal Web UI | Optional Deployment |
| Workers | Deployment (1+ replicas, scales horizontally) |
| FastAPI | Deployment (existing) |

Ephemeral agent pods are K8s Jobs created by the `run_agent_step` activity via `KubernetesSpawner`.

### Podman

**Podman is a supported but operationally simpler deployment target.** Temporal Server runs single-instance via podman-compose — no HA, no multi-replica. This matches the existing Podman deployment model where the entire stack runs on a single host.

```yaml
services:
  temporal:
    image: temporalio/auto-setup:latest
    environment:
      DB: postgresql
      DB_PORT: 5432
      POSTGRES_USER: temporal
      POSTGRES_PWD: temporal
      POSTGRES_SEEDS: postgresql
    ports:
      - "7233:7233"
    depends_on:
      - postgresql
  temporal-ui:
    image: temporalio/ui:latest
    environment:
      TEMPORAL_ADDRESS: temporal:7233
    ports:
      - "8080:8080"
  postgresql:
    image: postgres:16
    environment:
      POSTGRES_USER: temporal
      POSTGRES_PASSWORD: temporal
    volumes:
      - temporal-db:/var/lib/postgresql/data

volumes:
  temporal-db:
```

**Known Podman considerations:**
- `temporalio/auto-setup` runs schema migrations on first start. Tested with Podman 4.x on RHEL 9; rootless networking requires `--network slirp4netns` or a Podman network
- HA on Podman is out of scope — Podman deployments are single-host. This is consistent with how the existing stack deploys on Podman (single PostgreSQL, single FastAPI, no replication)
- Production Podman deployments should pin image tags and use `temporalio/server` (not `auto-setup`) after initial schema creation

**Temporal Lite alternative:** For minimal Podman deployments, `temporal server start-dev` provides a single-binary Temporal Server with SQLite storage (no PostgreSQL required). This reduces the Podman stack to: Temporal Lite binary + FastAPI + worker. Suitable for development and small-scale deployments where workflow volumes are low (<10 concurrent workflows).

### Operational Responsibility

| Environment | Who operates Temporal Server |
|---|---|
| Development / testing | Developer runs via compose |
| Staging | Platform team deploys via Helm/compose |
| Production (OCP) | Platform team or managed Temporal Cloud ($100/month) |
| Production (Podman) | Deployer runs single-instance via compose |
| Air-gapped / on-prem | Self-hosted; Temporal Cloud is NOT available |

## Honest Cost Accounting

### Lines deleted

| File | Lines | Fate |
|---|---|---|
| `workflow/executor.py` | 464 | Deleted |
| `workflow/step_dispatcher.py` | 214 | Deleted |
| `workflow/advancement.py` | 227 | Deleted |
| `workflow/persistence.py` | 130 | Deleted |
| `workflow/postgres_persistence.py` | 148 | Deleted |
| `workflow/graph_executor.py` | 269 | Deleted |
| `workflow/graph_state.py` | 45 | Deleted |
| `workflow/graph_steps.py` | 233 | Deleted |
| `workflow/graph_builder_factory.py` | 70 | Deleted |
| `workflow/parallel.py` | 84 | Deleted (validation logic moves to workflow) |
| `workflow/state.py` | 92 | Simplified (~40 lines kept for StepResult model) |
| `workflow/events.py` | 47 | Deleted |
| **Total deleted** | **~2,023** | |

### Lines added (realistic estimate)

| New File | Estimated Lines | Purpose |
|---|---|---|
| `workflow/temporal_workflow.py` | ~250 | Generic workflow class with step loop, conditions, approval, parallel groups |
| `workflow/temporal_activities.py` | ~150 | Agent execution activity (spawn/call/destroy), escalation, notification |
| `workflow/temporal_worker.py` | ~80 | Worker startup, workflow/activity registration |
| SSE bridge in API layer | ~60 | Query-polling SSE endpoint |
| Updated entrypoint/lifespan | ~50 | Replace RecoveryPoller with Temporal worker startup |
| **Total added** | **~590** | |

### Test migration

Current workflow test suite: **~6,800 lines** across unit and integration tests. These tests exercise the hand-rolled executor, persistence, and state management.

**Migration approach:**
- Tests for kept components (`conditions.py`, `auto_approve.py`, `advisory.py`, `permissions.py`, spawner) are unchanged (~3,000 lines)
- Tests for deleted components (executor, persistence, advancement, graph) are replaced with Temporal-specific tests using `WorkflowEnvironment` (~2,000 lines deleted, ~1,500 lines added)
- New Temporal-specific tests: workflow replay safety, signal delivery, parallel execution (~500 lines)
- **Net test change**: ~2,000 lines deleted, ~2,000 lines added — roughly neutral

### Net impact

- **Production code**: ~2,023 deleted, ~590 added = **~1,433 net reduction**
- **Test code**: ~2,000 deleted, ~2,000 added = **roughly neutral**
- **New infrastructure**: Temporal Server + PostgreSQL (PostgreSQL already required; Temporal Server is the net addition)

## 2 MB Payload Limit

Temporal has a 2 MB per-payload limit on activity inputs/outputs.

**Assessment for Cloud Agents:**
- Typical agent structured output: 1-50 KB (diagnostic reports, remediation plans)
- Escalation documents with full evidence from 5 steps: ~200-500 KB
- Largest realistic workflow (10 steps, each with rich output): ~1 MB

**If a workflow exceeds 2 MB**: Use the claim-check pattern — store large payloads in an object store (S3, MinIO, or local filesystem) and pass references through Temporal. This is a documented Temporal best practice, not a custom workaround.

**Current implementation does not need this** — structured Pydantic models are compact by design. Add the claim-check pattern as a backlog item if evidence-heavy escalation workflows emerge.

## Deterministic Workflow Constraints

Temporal workflows must be deterministic — no I/O, no `random()`, no `datetime.now()` in workflow code. This is enforced by the Python SDK sandbox (runtime detection of non-deterministic calls).

**Impact on Cloud Agents:**
- `evaluate_condition()` — already pure (string parsing, no I/O) ✓
- `interpolate()` (prompt templates) — already pure (string formatting) ✓
- `auto_approve()` — already pure (risk classification from step spec) ✓
- `build_escalation()` — has I/O (accessing step results); must be an activity, not inline

**Mitigation:**
- Document the constraint in the developer guide
- CI lint rule: any import of `datetime`, `random`, `os`, or network libraries in `temporal_workflow.py` fails the build
- All I/O (LLM calls, HTTP calls, file access, notification sends) is in activities, never in the workflow

**Team ramp**: ~2 weeks for developers already familiar with async Python. The constraint is conceptually similar to pure-function discipline in functional programming.

## Migration Path

### Phase 1: Temporal alongside existing executor (2-3 weeks)
- Add `temporalio` + `pydantic-ai[temporal]` dependencies
- Implement `AgentWorkflow` Temporal class
- Run Temporal Server via compose
- New `/v2/workflows/*` endpoints use Temporal; existing `/v1/` endpoints unchanged
- Validate behavioral parity on a single workflow definition
- **Rollback**: delete `/v2/` endpoints, remove Temporal dependency. Zero impact on v1.

### Phase 2: Feature parity + testing (2-3 weeks)
- Port all workflow definitions to Temporal execution
- Port approval gates to Temporal signals
- Port SSE to query polling
- Rewrite workflow tests against `WorkflowEnvironment`
- E2E tests on both Kind and Podman with Temporal
- **Rollback**: revert to v1 endpoints. Temporal workflow histories are not used by v1; no data migration needed.

### Phase 3: Remove hand-rolled code (1 week)
- Delete executor.py, persistence.py, advancement.py, graph_executor.py, and related modules
- Remove `/v1/` endpoints
- Update documentation
- **Rollback**: git revert. The deleted code is in version control.

## Secrets Management

Agent steps receive secrets differently depending on execution mode:

- **Spawned pods (production path)**: Secrets injected via `KubernetesSpawner` using `SecretKeyRef` (K8s Secrets) or via host env propagation (`PodmanSpawner`). The activity does not serialize or pass secrets through Temporal — it reads them from the runtime environment and passes them to the spawner.

- **Worker process**: The worker itself needs the Temporal Server address and optionally TLS certificates. These are env vars on the worker container, not passed through Temporal payloads.

- **Temporal payload safety**: No secrets are ever passed as workflow inputs, activity inputs, or signal payloads. Secrets stay in the runtime environment and are injected at the spawner layer.

## Day-2 Operations

### Temporal Server upgrades
Temporal supports rolling upgrades — workers can run a newer SDK version while the server is on the previous version (and vice versa within a compatibility window). In-flight workflows are not affected; they continue from their last checkpoint after the upgrade.

- **Schema migrations**: Required between some Temporal Server versions. Run via `temporal-sql-tool` or the `auto-setup` image. Test migrations on a staging database first.
- **Workflow versioning**: If workflow code changes while workflows are in-flight, use `workflow.patched()` to branch old/new execution paths. For Cloud Agents, this is rare — the generic `AgentWorkflow` class interprets YAML, so most changes are to YAML definitions, not workflow code.
- **Health monitoring**: Temporal Server exposes Prometheus metrics at `:9090/metrics`. Key alerts: `temporal_persistence_latency_bucket` (database health), `schedule_to_start_latency` (worker capacity), `workflow_failed` (workflow errors).

### Multi-tenancy
For shared OCP clusters where multiple product teams deploy agents:
- Use a **single Temporal namespace** per cluster (simplest). Team isolation comes from workflow ID prefixes and task queue separation.
- For stronger isolation, use **separate Temporal namespaces** per team. Each team's workers poll their own namespace. This adds operational complexity but prevents one team's workflows from affecting another's.
- Initial deployment: single namespace. Add namespace isolation if teams request it.

### Worker resource limits
- Workers run as Deployment replicas. Set K8s resource limits per container: recommended starting point is 512Mi memory, 500m CPU per worker.
- Configure `max_concurrent_activities` on the worker (default: 100). For Cloud Agents where each activity spawns a pod and makes an LLM call, a lower limit (10-20) prevents resource exhaustion.
- Workers do NOT hold LLM context in memory — the spawned pod handles that. Worker memory usage is bounded by Temporal SDK overhead + serialized payloads.

### Cold start latency
With spawned-pod-only execution, each agent step incurs container startup latency (typically 5-15 seconds for K8s Jobs, 2-5 seconds for Podman containers). Mitigations:
- **Pre-deployed agents** (`spawn: pre-deployed`): The activity calls a persistent service endpoint from the agent registry, skipping spawn/destroy entirely. Use for latency-sensitive steps.
- **Warm pools** (future): Pre-spawn N agent containers and assign on demand. Not implemented — add to backlog if cold start becomes a bottleneck.

### Exit strategy
If Temporal needs to be replaced post-migration, the Temporal-specific code surface is ~590 lines (workflow class, activities, worker startup). The agents, spawners, YAML definitions, conditions, approval logic, and all tool code are independent. Estimated effort to swap orchestrators: 2-3 weeks — write a new executor that reimplements the step loop, approval signals, and state persistence against a different backend.

## Goals Mapping

| Goal | How Temporal Architecture Addresses It |
|---|---|
| **G1: Bring your own agents** | Unchanged — YAML + tools.py → `create_generic_runner()`. Single generic `AgentWorkflow` interprets any YAML definition at runtime. |
| **G2: Multi-step workflows** | Temporal workflow class with sequential + parallel steps, conditions, signals, retry policies. Replaces both `WorkflowExecutor` and `GraphExecutor`. |
| **G3: Ephemeral execution** | `run_agent_step` activity uses existing spawner abstraction. Content-hash naming enables idempotent retry. Pre-deployed agents supported via `spawn: pre-deployed` (calls persistent service endpoint, skips spawn/destroy). |
| **G4: Dual deployment** | Temporal Server runs on both K8s (Helm) and Podman (compose). Temporal Lite available for minimal Podman deployments. |
| **G5: Stateless scaling** | Temporal workers are stateless; add replicas to scale. Single-threaded workflow execution eliminates CAS locking. |
| **G6: Observability** | Temporal Web UI for workflow visibility + existing OTel tracing + query-based SSE for real-time events. |
| **G7: Security** | Unchanged — secrets via K8s refs, auth tokens, permission scoping. No secrets in Temporal payloads. |
