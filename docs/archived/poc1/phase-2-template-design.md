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
  output_type: DiagnosticReport       # class name — checked in built-in registry first
  output_type_module: diagnostic_tools # optional — fallback: load class from this module
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

  skills:                              # skill directories to scan for SKILL.md files
    directories:
      - /app/skills
    # Note: all skills found in directories are activated.
    # Per-skill name filtering deferred to Phase 3.

  lifecycle:
    type: request-response             # request-response | periodic-loop
    # periodic-loop fields:
    # interval_seconds: 300
    # dispatch_to: diagnostic-agent    ← resolved via AgentRegistry, not direct URL

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
    dispatch_to: diagnostic-agent          # resolved via AgentRegistry
    on_dispatch_success:                   # optional post-dispatch hook
      module: monitoring_tools
      function: mark_hosts_healthy         # fn(alerts) → mutates local state

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
2. **`importlib` fallback** — if not in registry and `output_type_module` is specified in the YAML, load from that module

**YAML contract:**
- `output_type: DiagnosticReport` — resolved from built-in registry (no module needed)
- `output_type: MyCustomReport` + `output_type_module: my_tools` — resolved from `my_tools.MyCustomReport`

```python
OUTPUT_TYPE_REGISTRY = {
    "DiagnosticReport": DiagnosticReport,
    "MonitoringResult": MonitoringResult,
    "str": str,
}

def resolve_output_type(name: str, module_name: str | None = None) -> type:
    """Resolve output type by name. Built-in registry first, then importlib fallback."""
    if name in OUTPUT_TYPE_REGISTRY:
        return OUTPUT_TYPE_REGISTRY[name]
    if module_name:
        mod = importlib.import_module(module_name)
        cls = getattr(mod, name, None)
        if cls is not None and isinstance(cls, type):
            return cls
    raise ValueError(f"Unknown output_type '{name}'. Provide output_type_module for custom types.")
```

This makes the built-in path zero-config and the custom path explicit. No ambiguity about which fields are needed.

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

## Skills Activation

The generic entrypoint activates skills from the YAML definition:

1. Read `spec.skills.directories` — paths to scan for `SKILL.md` files
2. All discovered skills are activated (per-name filtering deferred to Phase 3)
3. Create a `SkillsCapability(directories=dirs)` and pass to the `Agent()` constructor via `capabilities=[...]`
4. **Strict startup**: if skills are configured but `pydantic-ai-skills` is not installed, **fail startup** — do not silently degrade
5. If no skills section is present in YAML, skills are simply not used (no error)

```python
def load_skills(spec: AgentSpec) -> list:
    """Load skills from configured directories. Strict — fails if skills are
    requested but the library is unavailable."""
    if not spec.skills or not spec.skills.directories:
        return []
    try:
        from pydantic_ai_skills import SkillsCapability
    except ImportError as exc:
        raise RuntimeError(
            "Skills are configured in agent.yaml but pydantic-ai-skills "
            "is not installed. Install it or remove the skills section."
        ) from exc
    for d in spec.skills.directories:
        if not Path(d).is_dir():
            raise RuntimeError(f"Skills directory not found: {d}")
    return [SkillsCapability(directories=spec.skills.directories)]
```

Skills directories are either:
- Baked into the image (production)
- Volume-mounted at `/app/skills/` (dev/test)

---

## Lifecycle Selection

The generic entrypoint reads `spec.lifecycle.type`:

- **`request-response`**: Standard `create_app()`, no background tasks
- **`periodic-loop`**: `create_app()` + `AgentLoop` as lifespan background task. `AgentLoop` is generalized from Phase 1b's `MonitoringLoop`.

### Post-dispatch hook

Periodic-loop agents can specify an `on_dispatch_success` callback:

```yaml
lifecycle:
  type: periodic-loop
  interval_seconds: 300
  dispatch_to: diagnostic-agent
  on_dispatch_success:
    module: monitoring_tools
    function: mark_hosts_healthy    # fn(alerts) → mutates local state
```

The callback is loaded via `importlib` (same contract as tools and validators). It receives the list of alerts that triggered dispatch and can mutate local state to prevent redispatch. If not specified, no post-dispatch action is taken.

### Dispatch routing

`dispatch_to` is resolved via `AgentRegistry` — the same config-driven registry from Phase 1. No direct endpoint URLs in the agent YAML.

**How the generic runtime obtains the registry:**

The agent pod reads registry data from a mounted YAML file at `/app/registry.yaml`:

```yaml
# /app/registry.yaml — mounted alongside agent.yaml
agents:
  - name: diagnostic-agent
    endpoint: http://diagnostic-agent:8080
  - name: monitoring-agent
    endpoint: http://monitoring-agent:8080
```

The generic entrypoint loads this at startup:

```python
REGISTRY_PATH = os.environ.get("AGENT_REGISTRY", "/app/registry.yaml")

def load_registry() -> AgentRegistry:
    if not Path(REGISTRY_PATH).exists():
        return AgentRegistry({})  # no dispatch capability
    with open(REGISTRY_PATH) as f:
        data = yaml.safe_load(f)
    return AgentRegistry({a["name"]: a["endpoint"] for a in data.get("agents", [])})
```

In Kind/Podman, `registry.yaml` is mounted from a ConfigMap or volume. The generic runtime does not call the core pod to discover agents — it reads a static file, consistent with Phase 1's config-driven discovery model.

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

Migration is not "constants to YAML" — it is "declarative config + Python hook modules preserving existing behavioral semantics."

1. **Build generic runtime** (new files only — `definition.py`, `tool_loader.py`, `output_types.py`, `generic_entrypoint.py`, `generic_runner.py`, `agent_loop.py`, `model_factory.py`)
2. **Extract declarative parts to YAML** — instructions, output_type, retries, lifecycle type, skill names, resource limits
3. **Preserve behavioral parts as Python hook modules** — tool functions stay in `tools.py`, output validators stay as Python functions, post-dispatch callbacks stay as Python functions. These are loaded via `importlib` at runtime.
4. **Build template Containerfile** — single image with mount points
5. **Verify semantic parity** — existing E2E tests pass with generic image running as both diagnostic and monitoring agents. Not just "it starts" but "it produces the same behavior."
6. **Deprecate per-agent Containerfiles** (keep for one release)

---

## TDD Task Breakdown

| Task | What | Tests | Est. |
|------|------|-------|------|
| 1. AgentDefinition model | Pydantic model for `agent.yaml` | Schema validation, YAML round-trip, enum validation | 1d |
| 2. Tool loader | `load_tools()` with module import + function resolution | Happy path, missing module, missing function, non-callable | 1d |
| 3. Output type registry | `resolve_output_type()` with known types | Known resolves, unknown raises ValueError | 0.5d |
| 4. Shared model factory | Move identical `_model.py`/`get_model()` to `src/agents/runtime/model_factory.py` | Env var handling, caching, API key passthrough | 0.5d |
| 5. Generic runner | `create_generic_runner()` builds agent + runner from spec | Success with FunctionModel, error path, tool registration | 1.5d |
| 6. Generic entrypoint | Reads YAML, assembles app, lifecycle branching, registry loading | Request-response starts, periodic-loop starts, missing YAML/registry errors | 1d |
| 7. Agent loop generalization | Extract `MonitoringLoop` → `AgentLoop` with configurable dispatch + post-dispatch hook | Start/stop, dispatch, failure survival, on_dispatch_success callback | 1d |
| 8. Template Containerfile + compose | Single image builds, mounts work | Manual: build, run as diagnostic, run as monitoring, E2E pass | 1d |
| 9. Migration verification | Existing E2E pass on generic image | All 9 existing E2E scenarios pass unchanged | 0.5d |

**Implementation order:** 1→2→3→4→5→6→7 (local code + tests) → 8→9 (containers + E2E).

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
- Per-skill name filtering (Phase 2 activates all skills in configured directories)
