# Temporal Migration — Replace Hand-Rolled Workflow Executor

## Context

The Cloud Agents framework (Phases 3-8) has a hand-rolled workflow executor with custom persistence, recovery polling, optimistic locking, and async callback dispatch. These are ~2,000 lines of orchestration code that reimplements what Temporal provides natively.

`temporal-architecture.md` proposes replacing the custom executor with Temporal while keeping the existing Pydantic AI agents, spawner abstraction, and YAML-driven configuration. This eliminates crash recovery, CAS race conditions, and orphaned-pod scenarios as custom bugs.

The migration follows the 3-phase plan from the design doc, running v2 endpoints alongside v1 before cutting over.

## What's Kept vs Deleted

**Kept (no changes)**: `conditions.py`, `auto_approve.py`, `advisory.py`, `permissions.py`, `interpolation.py`, `definition.py`, all spawners, all runtime code, `RemoteAgentClient`, `AgentRegistry`.

**Kept (refactored)**: `api.py` (new v2 endpoints), `entrypoint.py` (Temporal worker startup), `definition_store.py` (lightweight versioning), `state.py` (keep StepResult as read-only model).

**Deleted**: `executor.py`, `step_dispatcher.py`, `advancement.py`, `persistence.py`, `postgres_persistence.py`, `graph_executor.py`, `graph_state.py`, `graph_steps.py`, `graph_builder_factory.py`, `parallel.py`, `events.py`.

## Phase 1: Temporal Alongside Existing Executor

**Goal**: v2 endpoints working with Temporal, v1 unchanged. Zero risk to existing functionality.

### Task 1: Add Temporal dependency + infrastructure
- Add `temporalio` to `pyproject.toml`
- Create `deploy/podman/docker-compose.temporal.yaml` (Temporal Server + PostgreSQL)
- Create `deploy/kind/temporal.yaml` (Temporal Server Deployment + Service)
- Verify Temporal Server starts on both Podman and Kind

### Task 2: Temporal workflow class (`temporal_workflow.py`)
- Single `AgentWorkflow` class interpreting YAML at runtime
- `WorkflowInput` / `WorkflowOutput` Pydantic models
- Sequential step loop with condition evaluation (reuse `conditions.py`)
- Human-approval via `@workflow.signal` + `workflow.wait_condition()`
- Parallel groups via `asyncio.gather()` on activities
- `@workflow.query` for status (returns step results + events)
- Deterministic: no I/O, no datetime.now(), no random()

### Task 3: Temporal activities (`temporal_activities.py`)
- `run_agent_step()` — spawn → call → destroy lifecycle (reuse spawner + RemoteAgentClient)
- Content-hash naming for idempotent retry (reuse existing `compute_spawn_name` pattern)
- Pre-deployed agent path (skip spawn/destroy, call registry endpoint)
- `build_escalation_activity()` — wraps existing `build_escalation()` as activity
- `send_notification_activity()` — wraps existing notifier

### Task 4: Temporal worker (`temporal_worker.py`)
- Worker startup: register AgentWorkflow + activities
- Task queue configuration
- `max_concurrent_activities` from env var
- Spawner/registry injection into activities

### Task 5: v2 API endpoints in `api.py`
- `POST /v2/workflows/run` — start workflow via Temporal Client
- `POST /v2/workflows/{id}/approve` — send signal
- `GET /v2/workflows/{id}` — query workflow status
- `GET /v2/workflows/{id}/events` — SSE via query polling (1s interval)
- v1 endpoints remain unchanged

### Task 6: Updated entrypoint
- `build_temporal_app()` — FastAPI + Temporal Client + Worker in lifespan
- `WORKFLOW_ENGINE` env var: `temporal` | `legacy` (default: `legacy`)
- When `temporal`: start Temporal worker in lifespan, create v2 endpoints
- When `legacy`: existing behavior unchanged

### Task 7: Tests
- Unit tests for workflow class using `WorkflowEnvironment` (Temporal test harness)
- Unit tests for activities with mocked spawner
- API tests for v2 endpoints
- E2E: single workflow on Podman with Temporal Server

### Task 8: Review
- Submit to independent reviewer
- Address findings iteratively until LGTM

---

## Phase 2: Feature Parity + Testing

**Goal**: All workflow features verified on Temporal. Port remaining capabilities.

### Task 1: Port all workflow definitions
- Verify all example workflows execute correctly on v2 endpoints
- Diagnose-fix workflow with approval gate
- Ephemeral-diagnose workflow
- Multi-step parallel workflows

### Task 2: Retry + escalation via Temporal
- `RetryPolicy` on `execute_activity()` replaces custom `RetryContext`
- `ActivityError` catch triggers `build_escalation_activity()`
- Verify retry context (failure history) passes between attempts

### Task 3: Advisory mode + PermissionScope in activities
- Pass advisory/permission context to `run_agent_step` activity
- Reuse `AdvisoryEnforcer` and `PermissionScope` in activity code

### Task 4: Visibility labels + observability
- Pass workflow labels to spawner in activities
- OTel tracing coexists with Temporal's built-in tracing
- Prometheus metrics from activities

### Task 5: Definition Store on Temporal
- DefinitionStore submits/lists definitions without custom persistence
- Options: keep lightweight in-memory/file store, or use Temporal's own persistence

### Task 6: Rewrite workflow tests
- Delete tests for executor, persistence, advancement, graph
- New tests using `WorkflowEnvironment`:
  - Sequential workflow completion
  - Parallel step execution
  - Condition evaluation + skip
  - Approval signal delivery + timeout
  - Retry exhaustion → escalation
  - Advisory mode tool filtering
  - Workflow replay safety (determinism check)

### Task 7: E2E tests on both Kind and Podman
- Kind: Temporal Server + worker + 2-step workflow with ephemeral pods
- Podman: Temporal via compose + workflow execution
- Verify approval flow end-to-end

### Task 8: Review
- Submit to independent reviewer
- Address findings iteratively until LGTM

---

## Phase 3: Remove Hand-Rolled Code

**Goal**: Delete the old executor, persistence, and graph code. Clean cut.

### Task 1: Delete old modules
- Remove: `executor.py`, `step_dispatcher.py`, `advancement.py`, `persistence.py`, `postgres_persistence.py`, `graph_executor.py`, `graph_state.py`, `graph_steps.py`, `graph_builder_factory.py`, `parallel.py`, `events.py`, `executor_protocol.py`
- Remove `callback.py` from runtime (Temporal replaces async callback)
- Remove `pydantic-graph` from `pyproject.toml`

### Task 2: Promote v2 to v1
- Rename `/v2/workflows/*` → `/v1/workflows/*`
- Remove old v1 endpoint code
- Remove `WORKFLOW_ENGINE` env var (Temporal is the only engine)

### Task 3: Clean up entrypoint
- Remove `build_workflow_app()` (legacy path)
- `build_temporal_app()` becomes the default
- Remove RecoveryPoller, persistence factory

### Task 4: Simplify state.py
- Remove `WorkflowState` (Temporal manages workflow state)
- Keep `StepResult` as activity return type
- Remove `StepResultPayload` (no more ingest endpoint)
- Remove `derive_status()` (Temporal tracks status)

### Task 5: Delete old tests
- Remove all test files for deleted modules
- Verify remaining test suite passes

### Task 6: Update documentation
- Update ARCHITECTURE.md for Temporal architecture
- Update CREATING-AGENTS.md (deployment now includes Temporal)
- Update GOALS.md if needed
- Update architecture-visualization.html

### Task 7: Review
- Submit to independent reviewer
- Address findings iteratively until LGTM

---

## Key Design Decisions

1. **Single generic workflow class** — one `AgentWorkflow` interprets any YAML definition. No dynamic class generation. New definitions work without worker restarts.

2. **Spawned pods as default** — all agent steps use the spawner abstraction (K8s Jobs or Podman containers). Pre-deployed agents are a per-step config option, not a separate execution mode.

3. **SSE via query polling** — 1-second interval, no Redis. Acceptable because workflow steps take seconds to minutes.

4. **WorkflowState kept as read-only model** — `StepResult` and step dict structure preserved so `conditions.py` and `interpolation.py` work unchanged.

5. **v2 alongside v1** — Phase 1 runs both, Phase 3 cuts over. Clean rollback at any point.

## Verification

Each phase includes:
- Unit tests (TDD — tests first)
- E2E tests on both Kind and Podman
- Independent reviewer sign-off
- `uv run pytest tests/unit/agents/ -q` must pass
- `uv run make verify` must pass
