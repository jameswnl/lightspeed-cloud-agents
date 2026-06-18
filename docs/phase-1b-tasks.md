# Phase 1b: Cloud Agents — Task Breakdown (TDD)

## Context

Phase 1a delivered the cloud agent framework: diagnostic agent in a container, HTTP communication via `/v1/run` + `/healthz`, `RemoteAgentClient`, `AgentRegistry`, config integration, Containerfile, Kind/Podman deployment, and 72 unit tests + 4 E2E scenarios.

Phase 1b adds: monitoring agent, async `/v1/run`, liveness detection, structured observability, and full cross-pod E2E coverage.

**Deployment target:** Kind cluster + Podman (same as 1a)
**Simulated cluster:** Same as 1a — mutable dict, not real K8s APIs
**Approach:** TDD — write tests first, then implement
**LLM mocking:** Pydantic AI `FunctionModel` for unit tests

---

## Quality Checklist (lessons from Phase 1a reviews)

*Two independent 3rd-party reviews of Phase 1a exposed systematic patterns. These rules apply to every Phase 1b task.*

### For every task, before marking complete:

- [ ] **Test the seams** — every cross-component call has a boundary test from the *caller's* perspective, not just the callee's. If Task X calls Task Y's code, there's a test proving the contract from X's side.
- [ ] **Spec → test assertion** — every acceptance criterion has a corresponding test assertion. If you can't write the test, the criterion is too vague.
- [ ] **Test the startup** — every module that reads env vars, assembles the app, or does work at import time has at least one test exercising the startup path.
- [ ] **Assert behavior, not shape** — tests check what happened (state changed, action taken, error raised), not just what fields exist in the response.
- [ ] **Strict types from day one** — if the spec says a field is constrained (URL, enum, etc.), use the strictest Pydantic type. No `str` when `AnyHttpUrl` or `Literal[...]` is appropriate.

### Why each rule exists (traced to Phase 1a findings):

| Rule | Phase 1a failure it prevents |
|------|------------------------------|
| Test the seams | `RemoteAgentClient` silently accepted `success=False` — each side worked, the bug lived at the boundary |
| Spec → test assertion | Spec said "cross-pod HTTP communication" but E2E only tested direct HTTP to agent pod |
| Test the startup | `entrypoint.py` had 0% coverage, `_model.py` had 46% — real deployments fail at env var / import time |
| Assert behavior, not shape | E2E checked `cluster_healthy is present` — passed even when agent returned empty report |
| Strict types from day one | `endpoint: str` accepted `"not-a-url"`, `type: str` accepted `"typo"` — spec said "authoritative" but code accepted garbage |

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

## 3rd-Party Review (2026-06-18)

A 3rd-party reviewer identified 4 issues. Resolutions:

| # | Severity | Issue | Resolution |
|---|----------|-------|-----------|
| 1 | Blocker | Monitoring re-dispatches endlessly because its local state never reflects diagnostic's remediation | **Option B:** monitoring mutates its own local state after receiving a successful `DiagnosticReport` with `cluster_healthy=True`. Simple, no shared state service needed. |
| 2 | Major | `/livez` measures `/v1/run` activity, not loop health — idle monitoring agent looks dead | Monitoring loop updates heartbeat on every iteration (including idle). `/livez` checks loop heartbeat, not endpoint activity. |
| 3 | Major | Per-tool metrics (`agent_tool_calls_total`) have no instrumentation point defined | **Deferred to Phase 2.** Phase 1b keeps per-run metrics only (`ls_agent_runs_total`, `ls_agent_run_duration_seconds`). Per-tool needs a Pydantic AI callback mechanism. |
| 4 | Major | E2E dispatch verification is vague — no concrete observation path | Defined: monitoring `/v1/run` returns `dispatched_run_ids` → E2E polls diagnostic `/v1/runs/{id}` → both agents have separate configurable E2E base URLs. |

---

## Security Review (2026-06-18)

A security-focused reviewer identified 5 issues. Resolutions:

| # | Severity | Issue | Resolution |
|---|----------|-------|-----------|
| 1 | Blocker | No inter-agent auth model — new APIs are unauthenticated | Add explicit **Trust Boundary** section: all agent endpoints are trusted-internal-only, dev/test clusters only, no external ingress. Full auth deferred to Phase 2. |
| 2 | Major | More surface area (autonomous pod, async polling, metrics) but security deferred | Add **minimum containment baseline**: Services are ClusterIP only, monitoring→diagnostic direction only, `environment: dev-test-only` label on manifests. |
| 3 | Major | Run IDs used as implicit authorization for polling | Document: `run_id` is a lookup key, not an auth credential. Polling is trusted-internal. Phase 2 adds caller-scoped access. |
| 4 | Major | Correlation IDs accepted from callers without sanitization — log injection risk | Validate: max 128 chars, `[a-zA-Z0-9\-]` only, generate UUID if absent or invalid. |
| 5 | Medium | Per-tool metrics could expose operational shape | Already resolved: per-tool metrics deferred to Phase 2. `/metrics` documented as internal-only. |

### Trust Boundary (Phase 1b)

**All agent endpoints are internal-only, for dev/test clusters only.**

| Property | Phase 1b rule |
|----------|--------------|
| Network exposure | `ClusterIP` Services only — no NodePort, no Ingress, no external access |
| Traffic direction | Monitoring → diagnostic only. Diagnostic does not call monitoring. |
| Authentication | None. Endpoints are unauthenticated. Acceptable for dev/test only. |
| Run ID access control | Lookup key only, not an auth credential. Anyone on the cluster network can poll. |
| `/metrics` access | Internal-only. No auth. Label cardinality bounded (per-run, not per-tool). |
| Correlation ID handling | Server-side validation: max 128 chars, `[a-zA-Z0-9\-]`, generate UUID if absent/invalid. Never log raw nested context. |
| Deployment label | All manifests tagged `environment: dev-test-only` |
| Documentation | README and deployment guide state: "do not deploy outside dev/test clusters without Phase 2 security hardening" |

---

## Architectural Decisions (resolved by evaluator + 3rd-party + security reviews)

### Shared cluster state: Context-passing with local state mutation

**Decision:** No shared state service. Monitoring passes alert context to diagnostic via HTTP dispatch. Both pods pre-seed the same scenario via env var. After a successful diagnostic dispatch, monitoring mutates its own local state to reflect the fix (prevents repeated redispatch).

**Rationale:** Matches real-world pattern — monitoring detects, dispatches, and trusts the diagnostic result. The local state mutation prevents infinite dispatch loops without needing a shared database or TTL-based suppression.

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

**State mutation after dispatch:** When diagnostic returns `cluster_healthy=True`, the monitoring loop marks the affected hosts as healthy in its own local state. This prevents repeated redispatch of the same issue. (Resolved from 3rd-party review finding #1.)

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

**Security containment (from security review):**
- All Services are `ClusterIP` only (no NodePort, no Ingress)
- All manifests tagged with `environment: dev-test-only` label
- Monitoring-agent manifest documents: "monitoring calls diagnostic, not vice versa"

**Acceptance:** `./deploy/kind/setup.sh` → three pods running. Monitoring agent logs show periodic checks. On `bad_deploy` scenario, monitoring agent dispatches to diagnostic agent. All Services are ClusterIP only.

---

### Task 5: Liveness endpoint + run timeout

Detect hung agents and enforce run timeouts.

**Files to modify:**
- `src/agents/runtime/server.py` — add `/livez` endpoint with heartbeat tracking, `asyncio.wait_for()` around agent runs
- `tests/unit/agents/runtime/test_server.py` — 4 new tests: `/livez` returns 200 when healthy, 503 when stale, run timeout returns 500, run within timeout succeeds

**Liveness design:**
- Server tracks `last_heartbeat` timestamp
- For request-driven agents (diagnostic): updated on each run start and completion
- For loop-driven agents (monitoring): updated on every loop iteration, including idle ones (prevents false 503 during sleep intervals)
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

**Prometheus naming:** Follow existing `ls_*` prefix convention. Reuse error-tolerant wrapper pattern from `src/metrics/recording.py`.

**Metrics scope (Phase 1b — per-run only):**
- `ls_agent_runs_total{agent_name, status}` — counter
- `ls_agent_run_duration_seconds{agent_name}` — histogram

Per-tool metrics (`ls_agent_tool_calls_total{agent_name, tool_name}`) deferred to Phase 2 — needs a Pydantic AI callback/hook mechanism for tool-level instrumentation.

**Files to create/modify:**
- `src/agents/runtime/metrics.py` — Prometheus metric definitions using `ls_agent_*` prefix
- `src/agents/runtime/server.py` — inject `correlation_id`, add `/metrics` endpoint, instrument runs
- `tests/unit/agents/runtime/test_metrics.py` — 2 tests: counter increments, histogram records duration
- `tests/unit/agents/runtime/test_server.py` — 3 new tests: `X-Correlation-ID` in response, `/metrics` returns Prometheus format, counter increments after run

**TDD sequence:**
1. Write 6 tests
2. Implement metrics module and server instrumentation
3. Tests pass

**Correlation ID validation (from security review):**
- If absent in request context, generate server-side UUID
- If present, validate: max 128 chars, `[a-zA-Z0-9\-]` only
- If invalid, replace with generated UUID and log a warning
- Never log raw nested context dicts — only validated scalar fields

**Acceptance:** Every response includes `X-Correlation-ID` header (validated or generated). `/metrics` returns Prometheus text format (internal-only, per-run metrics). Logs include validated `correlation_id` and `agent_name`.

---

### Task 7: E2E tests — monitoring + diagnostic cross-pod scenarios

Extend E2E coverage to all three PoC scenarios across pods.

**Files to modify:**
- `tests/e2e/features/cloud_agents.feature` — add 4 new scenarios
- `tests/e2e/features/steps/cloud_agents.py` — add step implementations

**E2E observation path (from 3rd-party review):**
1. E2E step definitions use two configurable base URLs: `E2E_MONITOR_HOST`/`E2E_MONITOR_PORT` and `E2E_DIAG_HOST`/`E2E_DIAG_PORT`
2. Monitoring's `/v1/run` returns `MonitoringResult` including `dispatched_run_ids`
3. E2E polls diagnostic's `/v1/runs/{id}` using those IDs to verify dispatch reached the diagnostic agent
4. Assertions check behavior (actions taken, state changes), not just field presence

**New scenarios:**
```gherkin
Scenario: Monitoring agent health check
  When I GET monitoring agent "/healthz"
  Then status is 200 and agent_name is "monitoring-agent"

Scenario: Monitoring agent detects anomaly on degraded cluster
  When I POST to monitoring agent "/v1/run" with prompt "Check cluster health"
  Then the response has alerts with severity "high" or "critical"
  And cluster_healthy is false

Scenario: Monitoring dispatches to diagnostic and dispatch is verifiable
  When I POST to monitoring agent "/v1/run" with prompt "Check and dispatch"
  Then dispatched_run_ids is not empty
  When I poll diagnostic agent "/v1/runs/{dispatched_run_id}"
  Then the diagnostic run status is "completed"

Scenario: Full cross-pod flow — detect, dispatch, remediate
  When I POST to monitoring agent "/v1/run" with prompt "Full diagnostic cycle"
  Then dispatched_run_ids is not empty
  When I poll diagnostic agent "/v1/runs/{dispatched_run_id}"
  Then the diagnostic output has actions_taken not empty
  And the diagnostic output has cluster_healthy true
```

**Acceptance:** `make e2e-cloud-agents-full` runs 8 scenarios (4 existing + 4 new). All pass. Dispatch is verified via concrete run IDs, not log scraping.

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
