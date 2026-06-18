# Phase 1b: Cloud Agents — Task Breakdown (TDD)

## Context

Phase 1a delivered the cloud agent framework: diagnostic agent in a container, HTTP communication via `/v1/run` + `/healthz`, `RemoteAgentClient`, `AgentRegistry`, config integration, Containerfile, Kind/Podman deployment, and 72 unit tests + 4 E2E scenarios.

Phase 1b adds: monitoring agent, async `/v1/run`, liveness detection, structured observability, and full cross-pod E2E coverage.

**Deployment target:** Kind cluster + Podman (same as 1a)
**Simulated cluster:** Same as 1a — mutable dict, not real K8s APIs
**Approach:** TDD — write tests first, then implement
**LLM mocking:** Pydantic AI `FunctionModel` for unit tests

---

## Evaluator Review (2026-06-18)

An independent evaluator reviewed this plan. Key findings and resolutions:

| # | Severity | Issue | Resolution |
|---|----------|-------|-----------|
| 1 | Blocker | Prometheus: plan creates parallel `/metrics` with different naming (`agent_runs_total` vs LCS `ls_*` prefix) | Use `ls_agent_*` prefix, follow existing `src/metrics/recording.py` pattern |
| 2 | Blocker | RunStore: no concurrency design for async submit path | Use `asyncio.Lock`, reference existing `in_memory_context_store.py` pattern. Add lazy cleanup on access. |
| 3 | Blocker | Backward compat: no explicit gate that Phase 1a's 72 tests pass after Task 2 changes | Make "all existing tests pass with zero modifications" an explicit acceptance criterion for Task 2 |
| 4 | Major | MonitoringLoop: no lifespan function, no error handling for dispatch failures | Add lifespan context manager to entrypoint. Loop catches `AgentUnavailableError`/`AgentTimeoutError` without crashing. |
| 5 | Major | E2E Scenario 4: no verification mechanism for monitoring→diagnostic dispatch | Add `dispatched_run_ids` to `MonitoringResult` so E2E can verify dispatch by polling diagnostic agent |
| 6 | Major | Scenario init: `datetime.now()` in `simulate_bad_deploy()` produces different timestamps across pods | Use fixed timestamps in scenario seeds |
| 7 | Major | Task ordering: Tasks 2, 5, 6 all modify `server.py` — can't parallelize | Sequence Tasks 2→5→6 for server.py changes |
| 8 | Minor | MonitoringLoop tests: risk of flaky timing | Mock `asyncio.sleep`, test `_check_and_dispatch` standalone, test lifecycle separately with zero interval |
| 9 | Minor | Missing: no task to register monitoring agent in AgentRegistry config | Add to Task 4 |

---

## Architectural Decisions (resolved by evaluator review)

### Shared cluster state: Context-passing, not shared service

**Decision:** No shared state service. Monitoring agent passes its findings as context via the HTTP dispatch to the diagnostic agent. Both pods pre-seed the same scenario via env var.

**Rationale:** Matches real-world pattern — a monitoring agent in production passes alert context, not shared memory. No 3rd pod, Redis, or database needed. Both pods use the same `cluster_state.py` with a `CLUSTER_SCENARIO` env var that controls which state is loaded at startup.

**What changes:**
- `cluster_state.py` gets `init_scenario(name)` — selects pre-built state (healthy, bad_deploy, disk_growth)
- Both pods set the same scenario env var in deployment manifests
- `AgentRunRequest.context` carries the monitoring agent's findings to the diagnostic agent

### Async /v1/run: Submit + poll with backward compatibility

**Decision:** `Prefer: respond-async` header triggers async mode. Without the header, behavior is unchanged (sync, blocking — Phase 1a compatible).

**Design:**
- `POST /v1/run` with `Prefer: respond-async` → `202 Accepted` + `{"run_id": "<uuid>", "status": "running"}`
- `GET /v1/runs/{run_id}` → `{"run_id": "...", "status": "running|completed|failed", "result": <AgentRunResponse|null>}`
- Storage: In-memory `dict[str, RunState]` on the agent pod. Runs expire after 1 hour. No persistence — pod restart loses runs.
- `RemoteAgentClient` gets `run_async()` + `poll_run()` + `run_with_polling()` methods

**Backward compatibility:** Phase 1a's sync behavior is the default. Async is opt-in via header.

### Observability: Structured logging + Prometheus, not OpenTelemetry

**Decision:** Correlation IDs in logs + Prometheus counters/histograms. No OpenTelemetry in Phase 1b.

**Rationale:** Full OTel (spans, exporters, collector, Jaeger/Tempo) is multi-week and doesn't advance the core goal. Correlation IDs + `/metrics` give 80% of the traceability value.

**What's added:**
- `correlation_id` in `AgentRunRequest.context` — auto-generated if absent
- Every log line includes `agent_name`, `correlation_id`, `run_id`
- `X-Correlation-ID` response header
- Prometheus metrics: `agent_runs_total{agent_name, status}`, `agent_run_duration_seconds{agent_name}`, `agent_tool_calls_total{agent_name, tool_name}`
- `/metrics` endpoint on agent pods

---

## Task Breakdown (7 tasks)

### Task 1: Shared models + scenario init

Add monitoring models and scenario-based state initialization.

**Files to create/modify:**
- `src/agents/models.py` — add `MonitoringAlert`, `MonitoringResult`, `RunState`, `RunStatus`
- `src/agents/diagnostic/cluster_state.py` — add `init_scenario(name: str)` that pre-seeds state based on env var
- `tests/unit/agents/test_models.py` — add serialization tests for new models
- `tests/unit/agents/diagnostic/test_cluster_state.py` — add tests for `init_scenario()`

**Models:**
```python
class MonitoringAlert(BaseModel):
    host: str
    metric: str
    value: str
    severity: Literal["low", "medium", "high", "critical"]
    context: str
    recommended_action: str

class MonitoringResult(BaseModel):
    alerts: list[MonitoringAlert] = Field(default_factory=list)
    cluster_healthy: bool

class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class RunState(BaseModel):
    run_id: str
    status: RunStatus
    result: Optional[AgentRunResponse] = None
    created_at: str
```

**Scenarios for `init_scenario()`:**
- `"healthy"` — all hosts healthy, no alerts (default)
- `"bad_deploy"` — web-02 degraded, app crashed after deploy v2.3.1 (fixed timestamp, not `datetime.now()`)
- `"disk_growth"` — db-01 disk at 82%, trending up

**MonitoringResult extension** (for E2E verification):
```python
class MonitoringResult(BaseModel):
    alerts: list[MonitoringAlert] = Field(default_factory=list)
    cluster_healthy: bool
    dispatched_run_ids: list[str] = Field(default_factory=list)  # run_ids of dispatched diagnostic runs
```

**TDD sequence:**
1. Write tests: `MonitoringAlert` validates severity enum, `MonitoringResult` serializes with nested alerts, `RunState` status transitions, `init_scenario("bad_deploy")` produces degraded web-02
2. Implement models and `init_scenario()`
3. Tests pass. Existing 72 tests unbroken.

**Acceptance:** All new model tests pass. `init_scenario()` produces correct pre-built states.

---

### Task 2: Async /v1/run + polling endpoint

Add async submission and polling to the agent runtime server. Backward-compatible.

**Files to create/modify:**
- `src/agents/runtime/run_store.py` — `RunStore` class (in-memory dict, 1hr expiry, thread-safe)
- `src/agents/runtime/server.py` — add `Prefer` header handling, `GET /v1/runs/{run_id}`, inject `RunStore`
- `src/agents/remote_agent_client.py` — add `run_async()`, `poll_run()`, `run_with_polling()` methods
- `tests/unit/agents/runtime/test_run_store.py` — tests for store operations
- `tests/unit/agents/runtime/test_server.py` — add async-specific tests
- `tests/unit/agents/test_remote_agent_client.py` — add async client tests

**TDD sequence:**
1. Write `RunStore` tests: create run, get run, get unknown returns None, expired run returns None
2. Implement `RunStore`
3. Write server tests: async submit returns 202, poll running returns status, poll completed returns result, poll unknown returns 404, sync mode still works (no `Prefer` header)
4. Modify server
5. Write client tests: `run_async()` returns run_id, `poll_run()` returns status, `run_with_polling()` completes end-to-end, timeout during polling raises `AgentTimeoutError`
6. Modify `RemoteAgentClient`
7. All tests pass. Phase 1a server tests still pass unchanged.

**Concurrency:** `RunStore` uses `asyncio.Lock` for thread safety (same pattern as existing `in_memory_context_store.py`). Lazy cleanup on access — expired runs removed when next get() is called.

**Acceptance:** `POST /v1/run` without `Prefer` header = sync (Phase 1a behavior). With `Prefer: respond-async` = 202 + run_id. `GET /v1/runs/{run_id}` returns result when complete. **All 72 existing Phase 1a tests pass with zero modifications.**

---

### Task 3: Monitoring agent (tools + agent + dispatch loop)

Port the PoC monitoring agent to the production framework. Includes the periodic dispatch loop.

**Files to create:**
- `src/agents/monitoring/__init__.py`
- `src/agents/monitoring/agent.py` — `create_monitoring_agent()`, `run_monitoring()` (agent_runner)
- `src/agents/monitoring/tools.py` — `get_cluster_summary()` tool
- `src/agents/monitoring/loop.py` — `MonitoringLoop` class with `start()`/`stop()`, configurable interval, dispatches via `RemoteAgentClient` on critical alerts
- `src/agents/monitoring/entrypoint.py` — FastAPI app + monitoring loop as background task on startup
- `tests/unit/agents/monitoring/__init__.py`
- `tests/unit/agents/monitoring/test_tools.py` — 3 tests
- `tests/unit/agents/monitoring/test_agent.py` — 4 tests with FunctionModel
- `tests/unit/agents/monitoring/test_loop.py` — 5 tests with mocked RemoteAgentClient

**MonitoringLoop design:**
```python
class MonitoringLoop:
    def __init__(self, agent_runner, dispatch_client: RemoteAgentClient, interval: int = 300):
        ...
    async def start(self):
        """Run monitoring loop as asyncio task."""
    async def stop(self):
        """Graceful shutdown."""
    async def _check_and_dispatch(self):
        """Run one monitoring cycle. Dispatch if critical alerts found."""
```

**TDD sequence:**
1. Write tool tests → implement `get_cluster_summary()`
2. Write agent tests with FunctionModel → implement `create_monitoring_agent()` with `MonitoringResult` output type
3. Write loop tests (mock `RemoteAgentClient`) → implement `MonitoringLoop`
4. Wire entrypoint

**Loop error handling:** `_check_and_dispatch()` catches `AgentUnavailableError` and `AgentTimeoutError` and logs them without killing the loop. The loop continues to the next interval.

**Entrypoint lifespan:** `entrypoint.py` uses an `asynccontextmanager` lifespan that starts the loop on startup and calls `stop()` + awaits completion on shutdown (SIGTERM).

**Test strategy for loop:** Mock `asyncio.sleep` to return immediately. Test `_check_and_dispatch()` as a standalone coroutine. Test `start()`/`stop()` lifecycle separately with zero interval to avoid flaky timing.

**Acceptance:** Monitoring agent produces `MonitoringResult`. Loop dispatches via `RemoteAgentClient` when severity >= high. Loop respects `stop()`. Loop survives dispatch failures. 12 new tests pass.

---

### Task 4: Monitoring agent container + updated deployment

Containerize the monitoring agent. Update manifests.

**Files to create/modify:**
- `deploy/monitoring-agent/Containerfile` — same pattern as diagnostic-agent
- `deploy/kind/monitoring-agent.yaml` — Deployment + Service with env vars: `DISPATCH_ENDPOINT`, `MONITOR_INTERVAL`, `CLUSTER_SCENARIO`
- `deploy/kind/setup.sh` — add monitoring-agent build + load + apply
- `deploy/podman/docker-compose.cloud-agents.yaml` — add monitoring-agent service with `depends_on: diagnostic-agent`

**Acceptance:** `./deploy/kind/setup.sh` → three pods running. Monitoring agent logs show periodic checks. On `bad_deploy` scenario, monitoring agent dispatches to diagnostic agent.

---

### Task 5: Liveness endpoint + run timeout

Detect hung agents and enforce run timeouts.

**Files to modify:**
- `src/agents/runtime/server.py` — add `/livez` endpoint with heartbeat tracking, `asyncio.wait_for()` around agent runs
- `tests/unit/agents/runtime/test_server.py` — 4 new tests: `/livez` returns 200 when healthy, 503 when stale, run timeout returns 500, run within timeout succeeds

**Liveness design:**
- Server tracks `last_heartbeat` timestamp, updated on each run start and completion
- `/livez` returns 503 if `now - last_heartbeat > 2 * configured_timeout`
- K8s liveness probe hits `/livez`

**TDD sequence:**
1. Write 4 tests
2. Implement `/livez` + timeout wrapper
3. Tests pass

**Acceptance:** `/livez` detects hung agents. Runs exceeding timeout return 500.

---

### Task 6: Structured logging + Prometheus metrics

Add correlation IDs and Prometheus counters.

**Prometheus naming:** Follow existing `ls_*` prefix convention (e.g. `ls_agent_runs_total`, `ls_agent_run_duration_seconds`). Reuse error-tolerant wrapper pattern from `src/metrics/recording.py`.

**Files to create/modify:**
- `src/agents/runtime/metrics.py` — Prometheus metric definitions using `ls_agent_*` prefix
- `src/agents/runtime/server.py` — inject `correlation_id`, add `/metrics` endpoint, instrument runs
- `tests/unit/agents/runtime/test_metrics.py` — 3 tests: counter increments, histogram records, tool counter increments
- `tests/unit/agents/runtime/test_server.py` — 3 new tests: `X-Correlation-ID` in response, `/metrics` returns Prometheus format, counter increments after run

**TDD sequence:**
1. Write 6 tests
2. Implement metrics module and server instrumentation
3. Tests pass

**Acceptance:** Every response includes `X-Correlation-ID` header. `/metrics` returns Prometheus text format. Logs include `correlation_id` and `agent_name`.

---

### Task 7: E2E tests — monitoring + diagnostic cross-pod scenarios

Extend E2E coverage to all three PoC scenarios across pods.

**Files to modify:**
- `tests/e2e/features/cloud_agents.feature` — add 4 new scenarios
- `tests/e2e/features/steps/cloud_agents.py` — add step implementations

**New scenarios:**
```gherkin
Scenario: Monitoring agent health check
Scenario: Monitoring agent detects anomaly
Scenario: Monitoring agent dispatches to diagnostic agent (verify via correlation ID)
Scenario: Full cross-pod flow — monitoring detects, diagnostic remediates, verify via poll
```

**Acceptance:** `make e2e-cloud-agents-full` runs 8 scenarios (4 existing + 4 new). All pass.

---

## Implementation Order

```
Task 1: Shared models + scenarios    ← foundation
  ↓
Task 2: Async /v1/run + polling      ← enables reliable dispatch (modifies server.py)
  ↓
Task 5: Liveness + timeout           ← hardening (modifies server.py — sequence after Task 2)
  ↓
Task 6: Logging + metrics            ← observability (modifies server.py — sequence after Task 5)
  ↓
Task 3: Monitoring agent             ← core new agent (depends on async client from Task 2)
  ↓
Task 4: Container + deploy           ← infrastructure (+ register monitoring agent in config)
  ↓
Task 7: E2E tests                    ← validate everything across pods
```

Tasks 1-3, 5-6: Local code + unit tests (no containers).
Tasks 2→5→6: Sequenced — all modify `server.py`, can't parallelize.
Task 4: Container build + deployment.
Task 7: E2E against deployed cluster.

## Verification

**Unit tests:**
```bash
uv run pytest tests/unit/agents/ -v  # ~110 tests (72 existing + ~38 new)
```

**Container builds:**
```bash
podman build -f deploy/monitoring-agent/Containerfile -t monitoring-agent:latest .
```

**Kind deployment:**
```bash
./deploy/kind/setup.sh   # 3 pods: ollama, diagnostic-agent, monitoring-agent
kubectl get pods
kubectl logs deployment/monitoring-agent  # periodic checks visible
```

**E2E tests:**
```bash
make e2e-cloud-agents-full  # 8 scenarios
```

## What's deferred to Phase 2

- OpenTelemetry distributed tracing (Phase 1b uses correlation IDs + Prometheus)
- Real cluster APIs (stays simulated)
- NetworkPolicy / ServiceAccount RBAC
- SSE streaming for progress visibility
- Run state persistence (in-memory only in Phase 1b)
- Wiring into `/query` endpoint (blocked on LCORE-2310)

## Effort estimate

~7.5 engineering days for all 7 tasks. With evaluator reviews per task, ~9-10 days total.
