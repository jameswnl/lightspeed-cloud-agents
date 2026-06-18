# Phase 2: Generic Agent Runtime Template Image — Design

**Date**: 2026-06-18
**Prerequisite**: Phase 1a + 1b complete
**Companion**: `cloud-agents.md` (Phase 2 section), `phase-1b-tasks.md`

---

## Problem

Phase 1a/1b built two agent types (diagnostic, monitoring) as separate images. Code comparison reveals near-total duplication:

- `_model.py`: 100% identical between both agents
- Runtime server, RunStore, RemoteAgentClient, metrics, correlation: all shared
- Agent runner function: 95% identical (same error handling, logging pattern)
- Containerfile: structurally identical, differ only in CMD and env vars
- Per-agent unique: tools, instructions, output_type, lifecycle, retries, output_validator

**Goal**: ONE image that can be instantiated as any agent type by mounting/injecting agent definition, tools, and skills at deploy time.

---

## Agent Definition YAML Schema

The agent definition file lives at `/app/agent.yaml` inside the container (mounted or baked).

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: diagnostic-agent

spec:
  instructions: |
    You are a cluster diagnostic and remediation agent.
    ...
  output_type: DiagnosticReport       # class name from output type registry
  retries: 3                           # default: 1
  defer_model_check: true              # default: true

  tools:
    module: diagnostic_tools           # Python module name under /app/tools/
    functions:
      - list_hosts
      - check_host
      - get_alerts
      - get_recent_deploys
      - run_remediation

  skills:
    - openshift-troubleshooting
    - root-cause-analysis

  lifecycle:
    type: request-response             # request-response | periodic-loop
    # periodic-loop fields:
    # interval_seconds: 300
    # dispatch_to: diagnostic-agent
    # dispatch_endpoint: http://diagnostic-agent:8080

  output_validator:                    # optional
    module: diagnostic_tools
    function: verify_all_fixed

  model:                               # optional — defaults to env vars
    name: granite-3.3

  resources:
    max_tokens_per_run: 50000
    timeout_seconds: 600
```

**Monitoring agent example** (periodic-loop lifecycle):

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: monitoring-agent

spec:
  instructions: |
    You are a cluster health monitoring agent. Detection only.
    ...
  output_type: MonitoringResult
  retries: 1

  tools:
    module: monitoring_tools
    functions:
      - get_cluster_summary

  lifecycle:
    type: periodic-loop
    interval_seconds: 300
    dispatch_to: diagnostic-agent
    dispatch_endpoint: http://diagnostic-agent:8080

  resources:
    timeout_seconds: 600
```

---

## Tool Loading Contract

Tools are Python code with imports. The contract:

1. `/app/tools/` is on `PYTHONPATH`
2. `agent.yaml` `tools.module` names a Python module importable from `/app/tools/`
3. Each function in `tools.functions` must exist in that module and be compatible with `agent.tool_plain()`
4. The generic entrypoint calls `importlib.import_module(spec.tools.module)` then `getattr(module, fn_name)`

**Tool loader** (`src/agents/runtime/tool_loader.py`):
```python
def load_tools(spec: ToolsSpec) -> list[tuple[str, Callable]]:
    module = importlib.import_module(spec.module)
    tools = []
    for fn_name in spec.functions:
        fn = getattr(module, fn_name, None)
        if fn is None:
            raise ToolLoadError(f"Function '{fn_name}' not found in '{spec.module}'")
        tools.append((fn_name, fn))
    return tools
```

**Tool dependency strategy**: Tools must use dependencies already in the base image, or a derived image must be built with additional `pip install` steps. Runtime `pip install` is not supported in Phase 2.

**Security**: `importlib.import_module` on mounted volumes is an arbitrary code execution vector. Mitigations:
- **Production**: use derived images with tools baked in (no runtime mounts of executable code)
- **Dev/test**: volume mounts acceptable, read-only mount recommended (`ro` flag)
- All manifests tagged `environment: dev-test-only` (per Phase 1b security review)
- Phase 3 may add tool signature verification or allowlisting

---

## Output Type Registry

Two resolution strategies, tried in order:

1. **Built-in registry** for known types (`DiagnosticReport`, `MonitoringResult`, `str`)
2. **`importlib` fallback** — if not in registry, load from the tools module using `output_type_module` + `output_type_class` fields in YAML

```python
OUTPUT_TYPE_REGISTRY = {
    "DiagnosticReport": DiagnosticReport,
    "MonitoringResult": MonitoringResult,
    "str": str,
}

def resolve_output_type(name: str, tools_module: str | None = None) -> type:
    if name in OUTPUT_TYPE_REGISTRY:
        return OUTPUT_TYPE_REGISTRY[name]
    if tools_module:
        mod = importlib.import_module(tools_module)
        cls = getattr(mod, name, None)
        if cls is not None and isinstance(cls, type):
            return cls
    raise ValueError(f"Unknown output_type '{name}'")
```

This avoids the hardcoded-only problem without requiring Phase 3 inline schemas. New agent types can define output models in their tools module.

---

## Output Validator Contract

Output validators are optional Python functions loaded via `importlib`. The contract:

**Required signature:**
```python
async def my_validator(ctx: RunContext[None], output: T) -> T:
    """Validate agent output. Raise ModelRetry to force re-run."""
    if not output.is_valid:
        raise ModelRetry("Fix this: ...")
    return output
```

- Takes `RunContext[None]` and the output model instance
- Returns the (possibly modified) output
- Raises `ModelRetry(message)` to send feedback to the LLM and retry
- Domain state (e.g., `cluster_state`) is accessed via module-level imports in the validator's module — not injected by the framework
- The generic runner wires the validator via `@agent.output_validator`

---

## Lifecycle Selection

The generic entrypoint reads `spec.lifecycle.type`:

- **`request-response`**: Standard `create_app()`, no background tasks
- **`periodic-loop`**: `create_app()` + `AgentLoop` as lifespan background task. `AgentLoop` is generalized from Phase 1b's `MonitoringLoop`.

---

## Containerfile Strategy

**ONE image, runtime configuration via mounted `agent.yaml` + `/app/tools/`.**

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
# ... standard uv + dependency install ...
COPY src/ src/
RUN uv sync --no-dev

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
RUN mkdir -p /app/tools /app/skills

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src:/app/tools" \
    AGENT_DEFINITION="/app/agent.yaml"

RUN useradd -r -u 1001 agent
USER 1001
EXPOSE 8080

CMD ["python", "-m", "agents.runtime.generic_entrypoint"]
```

**Deployment options:**
- Volume mount `agent.yaml` and tools for dev iteration
- Build derived image with tools baked in for production

---

## Directory Layout

```
src/agents/
  definition.py                      # NEW: AgentDefinition Pydantic model
  runtime/
    tool_loader.py                   # NEW: importlib-based tool loading
    output_types.py                  # NEW: OUTPUT_TYPE_REGISTRY
    agent_loop.py                    # NEW: generalized from monitoring/loop.py
    generic_entrypoint.py            # NEW: reads YAML, builds agent, starts app
    generic_runner.py                # NEW: generic run_agent() function
    server.py                        # EXISTING: unchanged
    run_store.py                     # EXISTING: unchanged
  diagnostic/                        # KEPT: for backward compat + tests
  monitoring/                        # KEPT: for backward compat + tests

agents/definitions/                  # Agent YAML definitions
  diagnostic-agent.yaml
  monitoring-agent.yaml

deploy/
  agent-runtime/
    Containerfile                    # NEW: single template image
```

---

## Migration Path

1. Build generic runtime (new files only — no existing code changes)
2. Create `agent.yaml` files from existing Python constants
3. Build template Containerfile
4. Verify existing E2E tests pass with generic image running as both agent types
5. Deprecate per-agent Containerfiles

---

## TDD Task Breakdown

| Task | What | Tests | Est. |
|------|------|-------|------|
| 1. AgentDefinition model | Pydantic model for `agent.yaml` | Schema validation, YAML round-trip, enum validation | 1d |
| 2. Tool loader | `load_tools()` with module import + function resolution | Happy path, missing module, missing function, non-callable | 1d |
| 3. Output type registry | `resolve_output_type()` with known types | Known resolves, unknown raises ValueError | 0.5d |
| 4. Generic runner | `create_generic_runner()` builds agent + runner from spec | Success with FunctionModel, error path, tool registration | 1.5d |
| 5. Generic entrypoint | Reads YAML, assembles app, lifecycle branching | Request-response starts, periodic-loop starts loop, missing YAML errors | 1d |
| 6. Agent loop generalization | Extract `MonitoringLoop` → `AgentLoop` with configurable dispatch | Start/stop, dispatch, failure survival, callback | 1d |
| 7. Template Containerfile + compose | Single image builds, mounts work | Manual: build, run as diagnostic, run as monitoring, E2E pass | 1d |
| 8. Shared model config | Move identical `_model.py`/`get_model()` to `src/agents/runtime/model_factory.py` | Env var handling, caching, API key passthrough | 0.5d |
| 9. Migration verification | Existing E2E pass on generic image | All 9 existing E2E scenarios pass unchanged | 0.5d |

**Implementation order:** 1→2→3→4→5→6 (local code + tests) → 7→8 (containers + E2E).

**Estimated effort:** ~8 engineering days, ~10 days with reviews.

---

## What's Deferred to Phase 3

- Dynamic output type registration (inline Pydantic schemas in YAML)
- Tool dependency installation at runtime (`requirements.txt` per tool)
- AI-generated agent definitions (Workflow Designer Agent)
- CRD-based K8s operator for agent deployment
- Per-tool Prometheus metrics
- MCP tool integration in `agent.yaml`
- Output validator as YAML rule (vs Python function)
- Hot-reload (change agent.yaml without pod restart)
