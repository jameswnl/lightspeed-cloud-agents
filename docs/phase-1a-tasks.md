# Phase 1a: Cloud Agents — Task Breakdown (TDD)

## Context

Phase 0 PoC validated that Pydantic AI supports multi-agent collaboration in a single process. Phase 1a moves the diagnostic agent into a separate container, communicating with the core pod via HTTP. The evaluator identified that this is where the real complexity lives — the in-process `diag_agent.run()` pattern breaks across pod boundaries.

**Deployment target:** Kind cluster + Podman
**Simulated cluster:** The cluster being diagnosed is simulated (mutable dict inside the agent). Our agent infrastructure (pods, HTTP) runs in real containers.
**Container strategy:** Two images:
1. **LCS image** — existing lightspeed-stack (core pod)
2. **Diagnostic agent image** — built from an agent runtime base pattern, with diagnostic tools and skills baked in. Genericization (one image for all agent types via mounts) is a Phase 2 concern.

**Approach:** TDD — write tests first, then implement to make them pass.
**LLM mocking for unit tests:** Use Pydantic AI's `FunctionModel` — avoids real LLM calls, runs fast, no Ollama needed.

---

## Evaluator Review Summary (2026-06-17)

An independent evaluator reviewed this plan. Key changes from the review:

| Issue | Severity | Resolution |
|-------|----------|------------|
| Tool loading mechanism hand-waved | Blocker | Phase 1a bakes diagnostic tools into the image. Generic tool mounting deferred to Phase 2. |
| Task 6 premature (LCORE-2310 not landed) | Blocker | Removed Task 6. Integration with `/query` happens after LCORE-2310. |
| Package structure conflicts with conventions | Major | All agent code under `src/agents/`. No repo-root `agents/` directory. |
| Container image not truly generic for 1a | Major | Build diagnostic-agent-specific image. "Generic runtime" is Phase 2. |
| E2E test infrastructure undefined | Major | Phase 1a E2E tests are manual-only (`make e2e-cloud-agents`). CI deferred. |
| `/run` endpoint unversioned | Minor | Use `/v1/run` and `/healthz`. |
| RemoteAgentTool naming | Minor | Renamed to `RemoteAgentClient`. |
| Config model addition not shown | Minor | Explicitly show `Optional[list[AgentEndpointConfig]]` in `Configuration`. |

---

## Readiness Review (2026-06-17)

A second independent evaluator reviewed implementation readiness. Findings and resolutions:

| Issue | Resolution |
|-------|-----------|
| Agent error types undefined | Created `src/agents/exceptions.py` with `AgentError`, `AgentTimeoutError`, `AgentUnavailableError` |
| FunctionModel mock pattern unclear | Added concrete callback example below |
| FastAPI dependency injection pattern unclear | Use `app.state.agent` + `Depends()`, override in tests via `app.dependency_overrides` |
| Test directory structure | `tests/unit/models/config/` already exists; `tests/unit/agents/` created |
| Containerfile base image | Use simple `python:3.12-slim`, not the UBI9/Konflux pattern (that's for the LCS image, not the agent image) |
| httpx dependency | Already in `[project] dependencies` (`httpx>=0.27.0`) — no change needed |
| Pre-existing pylint failure | `sentence_transformers` import error — pre-existing, not from our changes |

### Pre-work completed

```
src/agents/
├── __init__.py              ← created
├── exceptions.py            ← AgentError, AgentTimeoutError, AgentUnavailableError
├── runtime/
│   └── __init__.py          ← created
└── diagnostic/
    └── __init__.py          ← created

tests/unit/agents/
├── __init__.py              ← created
├── runtime/
│   └── __init__.py          ← created
└── diagnostic/
    └── __init__.py          ← created
```

### FunctionModel callback pattern (for Task 3 tests)

```python
from pydantic_ai.models.function import FunctionModel, AgentInfo
from pydantic_ai.messages import (
    ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
)

def diagnostic_mock(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Mock LLM that calls tools in sequence, then returns structured output."""
    # Check if this is the first call (no tool results yet) → call list_hosts
    has_tool_results = any(
        isinstance(part, ToolReturnPart)
        for msg in messages for part in getattr(msg, "parts", [])
    )
    if not has_tool_results:
        return ModelResponse(parts=[
            ToolCallPart(tool_name="list_hosts", args="{}", tool_call_id="call_1")
        ])
    # After tools have returned, produce final structured output
    return ModelResponse(parts=[TextPart(content='{"summary": "...", ...}')])

agent = Agent(FunctionModel(diagnostic_mock), output_type=DiagnosticReport, ...)
```

### FastAPI dependency injection pattern (for Task 2 tests)

```python
# In server.py:
from fastapi import Depends, FastAPI, Request

app = FastAPI()

def get_agent(request: Request) -> Agent:
    return request.app.state.agent

@app.post("/v1/run")
async def run_agent(body: AgentRunRequest, agent: Agent = Depends(get_agent)):
    result = await agent.run(body.prompt)
    ...

# In tests:
from fastapi.testclient import TestClient

app.state.agent = mock_agent  # or FunctionModel-based agent
client = TestClient(app)
resp = client.post("/v1/run", json={"prompt": "test"})
```

---

## Task Breakdown (8 tasks)

### Task 1: Shared agent models

Define the HTTP contract between core pod and agent pods.

**Files to create:**
- `src/agents/__init__.py`
- `src/agents/models.py` — `AgentRunRequest`, `AgentRunResponse`, `RemediationAction`, `DiagnosticReport`
- `tests/unit/agents/__init__.py`
- `tests/unit/agents/test_models.py`

**Models:**
```python
class AgentRunRequest(BaseModel):
    prompt: str                          # non-empty
    context: Optional[dict] = None       # correlation_id, trace_id, etc.

class AgentRunResponse(BaseModel):
    output: dict                         # agent's structured result (e.g., DiagnosticReport.model_dump())
    output_type: str                     # e.g. "DiagnosticReport" — identifies the output schema
    schema_version: str = "v1"           # forward-compatible versioning
    usage: dict                          # {"input_tokens": N, "output_tokens": N}
    agent_name: str
    success: bool
    error: Optional[str] = None

class RemediationAction(BaseModel):
    host: str
    action: str
    result: str
    success: bool

class DiagnosticReport(BaseModel):
    summary: str
    issues_found: list[str]
    actions_taken: list[RemediationAction]
    remaining_issues: list[str] = Field(default_factory=list)
    cluster_healthy: bool
```

**TDD sequence:**
1. Write tests: `AgentRunRequest` rejects empty prompt; `AgentRunResponse` round-trips through JSON; `DiagnosticReport` serializes with nested `RemediationAction`
2. Implement models
3. Tests pass

---

### Task 2: Agent runtime server

FastAPI app skeleton for agent pods. Not fully generic — but structured so Phase 2 can extract a base.

**Files to create:**
- `src/agents/runtime/__init__.py`
- `src/agents/runtime/server.py` — FastAPI app with `/v1/run` (POST) and `/healthz` (GET)
- `tests/unit/agents/runtime/__init__.py`
- `tests/unit/agents/runtime/test_server.py`

**Endpoints:**
- `GET /healthz` → `{"status": "ready"}` (200) or `{"status": "initializing"}` (503)
- `POST /v1/run` → accepts `AgentRunRequest`, returns `AgentRunResponse`

**TDD sequence:**
1. Write tests using FastAPI `TestClient`:
   - `/healthz` returns 200 with `{"status": "ready"}`
   - `/v1/run` with valid prompt + mocked agent → returns `AgentRunResponse`
   - `/v1/run` with empty prompt → 422
   - `/v1/run` when agent raises exception → 500 with error detail
2. Implement server. Agent is injected via dependency (not global) to enable mocking.
3. Tests pass

---

### Task 3: Diagnostic agent (tools + agent definition)

The diagnostic agent's tools and configuration, baked into the runtime.

**Files to create:**
- `src/agents/diagnostic/__init__.py`
- `src/agents/diagnostic/tools.py` — tool functions + simulated cluster state
- `src/agents/diagnostic/agent.py` — constructs the Pydantic AI `Agent` with tools, instructions, output_validator
- `src/agents/diagnostic/cluster_state.py` — simulated cluster state (extracted from PoC for testability)
- `tests/unit/agents/diagnostic/__init__.py`
- `tests/unit/agents/diagnostic/test_tools.py` — test each tool independently
- `tests/unit/agents/diagnostic/test_cluster_state.py` — test state mutations
- `tests/unit/agents/diagnostic/test_agent.py` — integration test with `FunctionModel`

**Tool loading:** Tools are registered via `@agent.tool_plain` decorators in `agent.py`, which imports from `tools.py`. No dynamic loading — the diagnostic agent's identity is baked in at the code level.

**Simulated state initialization:** `cluster_state.py` exposes `reset_cluster()`, `simulate_bad_deploy()`, `simulate_disk_growth()`. In the containerized version, state is initialized on process start (same as PoC). Each agent pod has its own process = its own state.

**TDD sequence:**
1. Write tool tests: `list_hosts()` returns 4 hosts; `check_host("unknown")` returns error; `run_remediation("web-02", "rollback_deploy:frontend", "reason")` mutates state
2. Write cluster state tests: `reset_cluster()` → all healthy; `simulate_bad_deploy()` → web-02 degraded
3. Implement tools and state (extract from `playground/try_server_agents.py`)
4. Write agent integration test using Pydantic AI `FunctionModel`: mock LLM returns tool call sequence → verify tools executed → verify `DiagnosticReport` is valid
5. Wire into the runtime server (Task 2's `/v1/run` delegates to this agent)
6. Tests pass

---

### Task 4: RemoteAgentClient (HTTP client in core pod)

The core pod's mechanism for calling agent pods over HTTP.

**Files to create:**
- `src/agents/remote_agent_client.py` — async HTTP client using `httpx.AsyncClient`
- `tests/unit/agents/test_remote_agent_client.py`

**API:**
```python
class RemoteAgentClient:
    def __init__(self, endpoint: str, timeout: float = 600.0):
        ...

    async def run(self, prompt: str, context: dict | None = None) -> AgentRunResponse:
        """POST to agent pod's /v1/run endpoint."""

    async def healthz(self) -> bool:
        """GET agent pod's /healthz endpoint."""
```

**TDD sequence:**
1. Write tests (mock `httpx.AsyncClient`):
   - Success: `run("investigate...")` → returns `AgentRunResponse` with `DiagnosticReport`
   - Timeout: → raises `AgentTimeoutError`
   - 500 error: → raises `AgentError` with detail
   - Malformed JSON: → raises `AgentError`
   - Connection refused: → raises `AgentUnavailableError`
   - `healthz()` returns True/False based on status code
2. Implement `RemoteAgentClient`
3. Tests pass

---

### Task 5: Agent registry + configuration

Core pod discovers and tracks available agent pods.

**Files to create:**
- `src/agents/registry.py` — `AgentRegistry` class
- `src/models/config.py` — add `AgentEndpointConfig`, `AgentResourceConfig` to existing config
- `tests/unit/agents/test_registry.py`
- `tests/unit/models/config/test_agent_endpoint_config.py`

**Config addition to `Configuration` class:**
```python
# In src/models/config.py, add to Configuration class:
agents: Optional[list[AgentEndpointConfig]] = None
```

**Config YAML:**
```yaml
agents:
  - name: diagnostic-agent
    endpoint: http://diagnostic-agent:8080
    type: diagnostic
    skills: [openshift-troubleshooting]
    resources:
      max_tokens_per_run: 50000
      timeout_seconds: 600
```

**TDD sequence:**
1. Write registry tests: `get_endpoint("diagnostic-agent")` → URL; `get_endpoint("unknown")` → `ValueError`; empty registry → `list_agents()` returns `[]`
2. Write config tests: parse valid YAML → `AgentEndpointConfig` with correct fields; invalid URL → validation error; missing name → validation error
3. Implement `AgentRegistry` and `AgentEndpointConfig`
4. Add `agents` field to `Configuration` in `src/models/config.py`
5. Tests pass

---

### Task 6: Diagnostic agent container image

Containerize the diagnostic agent using the runtime server + baked-in tools.

**Files to create:**
- `deploy/diagnostic-agent/Containerfile` — multi-stage build
- `deploy/diagnostic-agent/entrypoint.sh`

**Image structure:**
```
/app/
├── src/agents/runtime/server.py      ← FastAPI app
├── src/agents/diagnostic/            ← baked-in tools + agent
├── src/agents/models.py              ← shared models
└── examples/skills/openshift-troubleshooting/  ← skills (copied in)
```

**Environment variables:**
- `AGENT_NAME=diagnostic-agent`
- `OLLAMA_URL=http://ollama:11434/v1` (or whatever LLM backend)
- `SKILLS_DIR=/app/skills/`

**Acceptance:**
```bash
podman build -f deploy/diagnostic-agent/Containerfile -t diagnostic-agent:latest .
podman run -p 8080:8080 -e OLLAMA_URL=http://host.containers.internal:11434/v1 diagnostic-agent:latest
curl localhost:8080/healthz                    # → {"status": "ready"}
curl -X POST localhost:8080/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Check all hosts for issues"}' # → AgentRunResponse JSON
```

---

### Task 7: Kind + Podman deployment manifests

Deploy core pod + diagnostic agent pod.

**Files to create:**
- `deploy/kind/kind-config.yaml`
- `deploy/kind/diagnostic-agent.yaml` — K8s Deployment + Service
- `deploy/kind/ollama.yaml` — Ollama deployment (LLM backend for agents)
- `deploy/podman/docker-compose.cloud-agents.yaml` — compose: diagnostic-agent + ollama
- `deploy/kind/setup.sh` — create cluster, load images, apply manifests, wait for readiness
- `deploy/kind/teardown.sh` — clean up

**Kind setup script responsibilities:**
1. `kind create cluster --config kind-config.yaml`
2. Build images: `podman build` for diagnostic-agent
3. Load images: `kind load docker-image diagnostic-agent:latest`
4. Apply manifests: `kubectl apply -f diagnostic-agent.yaml -f ollama.yaml`
5. Wait for readiness: poll `/healthz` with retries
6. Print status: `kubectl get pods`

**Acceptance:** `./deploy/kind/setup.sh` → both pods running. `kubectl port-forward svc/diagnostic-agent 8080:8080` + `curl localhost:8080/healthz` works.

---

### Task 8: E2E tests — cross-pod communication

Validate the full flow across containers. **Manual-only for Phase 1a** (no CI pipeline integration).

**Files to create:**
- `tests/e2e/features/cloud_agents.feature`
- `tests/e2e/features/steps/cloud_agents.py`

**Prerequisite:** Kind cluster running via `deploy/kind/setup.sh`, or Podman compose via `deploy/podman/docker-compose.cloud-agents.yaml`.

**Scenarios:**
```gherkin
Feature: Cloud Agents cross-pod communication

  Background:
    Given The diagnostic agent pod is running
    And The diagnostic agent healthcheck returns 200

  Scenario: Diagnostic agent health check
    When I GET the diagnostic agent "/healthz"
    Then The response status is 200
    And The response body contains "ready"

  Scenario: Diagnostic agent responds to /v1/run
    When I POST to the diagnostic agent "/v1/run" with prompt "Check all hosts for issues"
    Then The response status is 200
    And The response contains a valid AgentRunResponse
    And The output contains "issues_found"

  Scenario: Diagnostic agent handles empty prompt
    When I POST to the diagnostic agent "/v1/run" with prompt ""
    Then The response status is 422

  Scenario: Diagnostic agent remediates and verifies
    When I POST to the diagnostic agent "/v1/run" with prompt "The cluster has alerts. Diagnose and fix all issues."
    Then The response status is 200
    And The output field "cluster_healthy" is true
    And The output field "actions_taken" is not empty
```

**Makefile target:**
```makefile
e2e-cloud-agents: ## Run cloud agent E2E tests (requires Kind cluster or Podman compose running)
	uv run behave --color --format pretty tests/e2e/features/cloud_agents.feature
```

**CI considerations:** Not in CI for Phase 1a. Documented as manual. CI integration deferred to Phase 1b when monitoring agent is added and the test surface is larger.

---

## Implementation Order (TDD)

```
Task 1: Shared models             ← foundation — write tests first
  ↓
Task 2: Agent runtime server      ← FastAPI skeleton — write tests first
  ↓
Task 3: Diagnostic agent          ← tools + agent — write tool tests first
  ↓
Task 4: RemoteAgentClient         ← HTTP client — write mock tests first
  ↓
Task 5: Registry + config         ← wiring layer — write tests first
  ↓
Task 6: Container image           ← build + manual verification
  ↓
Task 7: Kind/Podman deployment    ← manifests + setup script
  ↓
Task 8: E2E tests                 ← validate across pods (manual)
```

Tasks 1-5: Pure code with unit tests. Run locally, no containers, no LLM (FunctionModel mocks).
Task 6: Container build + manual curl verification.
Task 7: Infrastructure (Kind cluster, manifests).
Task 8: E2E tests against running cluster.

## Verification

**Unit tests (Tasks 1-5):**
```bash
uv run make test-unit
```

**Container build (Task 6):**
```bash
podman build -f deploy/diagnostic-agent/Containerfile -t diagnostic-agent:latest .
podman run -p 8080:8080 diagnostic-agent:latest
curl localhost:8080/healthz
curl -X POST localhost:8080/v1/run -H 'Content-Type: application/json' -d '{"prompt":"Check all hosts"}'
```

**Kind deployment (Task 7):**
```bash
./deploy/kind/setup.sh
kubectl get pods
kubectl port-forward svc/diagnostic-agent 8080:8080 &
curl localhost:8080/healthz
```

**E2E tests (Task 8):**
```bash
make e2e-cloud-agents
```

## Non-goals for Phase 1a

- Monitoring agent (Phase 1b)
- Periodic triggers (Phase 1b)
- Real human approval flow (Phase 1b)
- Real cluster APIs (simulated state only)
- Wiring into `/query` endpoint (blocked on LCORE-2310)
- Generic agent runtime image with dynamic tool loading (Phase 2)
- Security hardening — RBAC, network policies (Phase 1b)
- Observability — OpenTelemetry, correlation IDs (Phase 1b)
- CI pipeline for E2E tests (Phase 1b)
- User-defined agents (Phase 2)
- AI-generated workflows (Phase 3)
