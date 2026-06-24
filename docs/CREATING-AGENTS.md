# Creating New Agent Workflows

This guide shows how to create and deploy a new agent type on the cloud agents platform. **No platform code changes required** — you write a YAML definition + a Python tool module, mount them onto the generic `agent-runtime` image, and run.

## What You Need

1. **An agent definition** (`agent.yaml`) — instructions, tools, output type, lifecycle
2. **A tools module** (`my_tools.py`) — Python functions the agent can call
3. **The `agent-runtime:latest` image** — already built, shared by all agents

That's it. No Containerfile, no PRs, no rebuilds.

---

## Step-by-Step: Create a Deployment Readiness Agent

This example creates an agent that checks whether a cluster is safe for a new deployment. It's completely independent of the existing diagnostic and monitoring agents.

### Step 1: Define the output model

Your tools module can define a custom Pydantic output model. The generic runtime loads it via `importlib` from the `output_type_module` field in your YAML.

**`my_tools/readiness_tools.py`:**

```python
from pydantic import BaseModel, Field

class DeploymentReadiness(BaseModel):
    """Custom output type — the agent returns this structure."""
    ready: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommendation: str
```

### Step 2: Write the tool functions

Tools are plain Python functions. The agent calls them via the LLM's tool-calling mechanism. Each function needs a docstring (the LLM reads it to decide when to call the tool).

**`my_tools/readiness_tools.py`** (continued):

```python
# Tools are plain Python functions — import what you need

def check_resource_capacity() -> list[dict]:
    """Check resource headroom across all hosts.

    Returns each host with current usage and whether it has
    enough capacity for a new deployment (CPU < 70%, memory < 75%, disk < 80%).
    """
    results = []
    for name, h in cluster_state["hosts"].items():
        results.append({
            "hostname": name,
            "cpu": h["cpu"],
            "memory": h["memory"],
            "disk": h["disk"],
            "has_capacity": h["cpu"] < 70 and h["memory"] < 75 and h["disk"] < 80,
        })
    return results


def check_active_incidents() -> dict:
    """Check for active alerts or recent failed deployments."""
    alerts = cluster_state.get("alerts", [])
    return {
        "active_alerts": len(alerts),
        "alert_details": alerts,
        "has_incidents": len(alerts) > 0,
    }


def check_service_health() -> list[dict]:
    """Check if all services across all hosts are running."""
    results = []
    for name, h in cluster_state["hosts"].items():
        for svc, status in h.get("services", {}).items():
            results.append({
                "hostname": name,
                "service": svc,
                "status": status,
                "healthy": status == "running",
            })
    return results
```

### Step 3: Write the agent definition YAML

This tells the generic runtime what agent to build — instructions for the LLM, which tools to load, what output type to expect, and the lifecycle (request-response vs periodic loop).

**`my_agents/deploy-readiness-agent.yaml`:**

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: deploy-readiness-agent

spec:
  # Instructions for the LLM — what the agent does and how
  instructions: |
    You are a deployment readiness agent. Your job is to assess whether
    a cluster is safe for a new deployment.

    Use check_resource_capacity to verify hosts have enough headroom.
    Use check_active_incidents to verify no ongoing issues.
    Use check_service_health to verify all services are running.

    Return a DeploymentReadiness report with:
    - ready: true/false
    - blockers: list of reasons deployment should NOT proceed
    - warnings: list of non-blocking concerns
    - recommendation: your final recommendation

    Be conservative — if anything is degraded, recommend waiting.

  # Output type — name of the Pydantic model class
  # Built-in types (DiagnosticReport, MonitoringResult, str) need no module.
  # Custom types need output_type_module pointing to the tools file.
  output_type: DeploymentReadiness
  output_type_module: readiness_tools

  # How many times to retry if the LLM output doesn't match the schema
  retries: 1

  # Tool loading — Python module + function names to register
  tools:
    module: readiness_tools        # importable from /app/tools/
    functions:
      - check_resource_capacity
      - check_active_incidents
      - check_service_health

  # Lifecycle — request-response (on-demand) or periodic-loop (autonomous)
  lifecycle:
    type: request-response

  # Resource limits
  resources:
    max_tokens_per_run: 30000
    timeout_seconds: 120
```

### Step 4: Run it

```bash
podman run -d --name readiness-agent \
  -p 8083:8080 \
  -v $PWD/my_agents/deploy-readiness-agent.yaml:/app/agent.yaml:ro \
  -v $PWD/my_tools/readiness_tools.py:/app/tools/readiness_tools.py:ro \
  -e OLLAMA_URL=https://api.openai.com/v1 \
  -e AGENT_MODEL=gpt-4o-mini \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  agent-runtime:latest
```

### Step 5: Test it

```bash
# Health check
curl http://localhost:8083/healthz
# {"status":"ready","agent_name":"deploy-readiness-agent"}

# Ask if the cluster is ready to deploy
curl -s -X POST http://localhost:8083/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Is the cluster ready for deploying frontend v2.4.0?"}' \
  | python3 -m json.tool
```

**Result on a degraded cluster:**

```json
{
    "output": {
        "ready": false,
        "blockers": [
            "web-02 is over capacity (CPU at 92%) and has an application crash.",
            "web-02's app service is currently crashed."
        ],
        "warnings": [
            "There is an ongoing alert regarding CPU spike on web-02."
        ],
        "recommendation": "Wait until web-02 issues are resolved before deploying."
    },
    "output_type": "DeploymentReadiness",
    "agent_name": "deploy-readiness-agent",
    "success": true
}
```

**Result on a healthy cluster** (change `AGENT_BOOTSTRAP_ARGS=healthy`):

```json
{
    "output": {
        "ready": true,
        "blockers": [],
        "warnings": [],
        "recommendation": "You can proceed with deploying frontend v2.4.0."
    },
    "output_type": "DeploymentReadiness",
    "agent_name": "deploy-readiness-agent",
    "success": true
}
```

---

## YAML Schema Reference

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `apiVersion` | string | Always `lightspeed.redhat.com/v1alpha1` |
| `kind` | string | Always `AgentDefinition` |
| `metadata.name` | string | Agent name (shown in healthz, logs, metrics) |
| `spec.instructions` | string | System prompt — what the agent does and how |
| `spec.output_type` | string | Class name of the output model |
| `spec.tools.module` | string | Python module name (importable from `/app/tools/`) |
| `spec.tools.functions` | list[string] | Function names to register as tools |
| `spec.lifecycle.type` | string | `request-response` or `periodic-loop` |

### Optional fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `spec.output_type_module` | string | none | Python module containing the custom output type class |
| `spec.retries` | int | 1 | Retries on output validation failure |
| `spec.defer_model_check` | bool | true | Skip model name validation (needed for non-OpenAI models) |
| `spec.output_validator.module` | string | none | Module containing the validator function |
| `spec.output_validator.function` | string | none | Validator function name: `async (ctx, output) -> output` |
| `spec.skills.directories` | list[string] | none | Paths to scan for SKILL.md files |
| `spec.lifecycle.interval_seconds` | int | 300 | Polling interval for periodic-loop agents |
| `spec.lifecycle.dispatch_to` | string | none | Agent name to dispatch to (resolved via registry) |
| `spec.lifecycle.on_dispatch_success.module` | string | none | Post-dispatch callback module |
| `spec.lifecycle.on_dispatch_success.function` | string | none | Post-dispatch callback: `fn(alerts) -> None` |
| `spec.model.name` | string | env var | Override LLM model name |
| `spec.resources.max_tokens_per_run` | int | 50000 | Token budget per run |
| `spec.resources.timeout_seconds` | int | 600 | Max run duration in seconds |

---

## Tool Module Contract

Your tools module (`/app/tools/my_tools.py`) must follow these rules:

1. **Functions are plain Python** — no decorators needed. The runtime registers them via `agent.tool_plain()`.

2. **Each function needs a docstring** — the LLM reads it to decide when to call the tool. Be specific about what the function does and what it returns.

3. **Return types should be JSON-serializable** — dicts, lists, strings, numbers. Pydantic models work too.

4. **Custom output types** go in the same module — define a `BaseModel` subclass and reference it via `output_type` + `output_type_module` in the YAML.

5. **Dependencies** must be in the base image — the tool module is mounted, not installed. If you need extra pip packages, build a derived image.

6. **Output validators** follow the Pydantic AI contract:
   ```python
   async def my_validator(ctx: RunContext[None], output: MyModel) -> MyModel:
       if not output.is_valid:
           raise ModelRetry("Fix this...")
       return output
   ```

---

## Lifecycle Types

### Request-Response (on-demand)

The agent waits for HTTP requests. Each `POST /v1/run` runs the agent once and returns the result.

```yaml
lifecycle:
  type: request-response
```

Use for: diagnostic agents, readiness checks, one-shot analysis.

### Periodic Loop (autonomous)

The agent runs a background loop at a configurable interval. When it detects issues, it dispatches to another agent via HTTP.

```yaml
lifecycle:
  type: periodic-loop
  interval_seconds: 60
  dispatch_to: diagnostic-agent           # resolved via registry
  on_dispatch_success:
    module: my_tools
    function: mark_issues_resolved        # post-dispatch state cleanup
```

Use for: monitoring agents, watchdogs, health checkers.

The dispatch target is resolved via `registry.yaml` (mounted at `/app/registry.yaml`):

```yaml
agents:
  - name: diagnostic-agent
    endpoint: http://diagnostic-agent:8080
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_DEFINITION` | `/app/agent.yaml` | Path to agent definition |
| `AGENT_REGISTRY` | `/app/registry.yaml` | Path to agent endpoint registry |
| `AGENT_MODEL` | `qwen3.6:latest` | LLM model name |
| `OLLAMA_URL` | `http://localhost:11434/v1` | LLM backend URL |
| `OPENAI_API_KEY` | `not-needed` | API key for OpenAI/Azure |
| `MONITOR_INTERVAL` | from YAML | Override loop interval |
| `AGENT_BOOTSTRAP_MODULE` | none | Optional startup hook module |
| `AGENT_BOOTSTRAP_FUNCTION` | none | Optional startup hook function |
| `AGENT_BOOTSTRAP_ARGS` | none | Optional startup hook arguments |

---

## Mount Points

| Container path | What to mount | Required |
|---------------|--------------|----------|
| `/app/agent.yaml` | Agent definition YAML | Yes |
| `/app/tools/` | Python tool modules | Yes |
| `/app/skills/` | SKILL.md directories | Only if skills configured |
| `/app/registry.yaml` | Agent endpoint registry | Only if lifecycle uses `dispatch_to` |

---

## Best Practice: Diagnose → Propose → Gate → Execute → Verify

The recommended pattern for remediation workflows follows 5 phases (from KubeKlaw learnings). Each phase is a regular workflow step — no special step types needed.

```yaml
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: remediation-workflow
spec:
  steps:
    # Phase 1: Diagnose — gather evidence, identify root cause
    - name: diagnose
      type: agent
      agent: diagnostic-agent
      prompt: "Investigate the reported issue. Identify root cause with confidence level."
      output_key: diagnosis

    # Phase 2: Propose — present options with risk and rollback
    - name: propose
      type: agent
      agent: diagnostic-agent
      prompt: |
        Based on diagnosis: {{ steps.diagnosis.output.summary }}
        Propose remediation actions with risk levels and rollback plans.
      output_key: proposal

    # Phase 3: Gate — human reviews before execution
    - name: approve
      type: human-approval
      message: "Review proposed actions and approve execution."
      output_key: approval

    # Phase 4: Execute — carry out the approved plan
    - name: execute
      type: agent
      agent: diagnostic-agent
      prompt: |
        Execute the approved remediation: {{ steps.proposal.output.actions }}
        Follow rollback plan if any step fails.
      output_key: execution
      condition: "steps.approval.output.approved == true"

    # Phase 5: Verify — independent agent confirms the fix worked
    - name: verify
      type: agent
      agent: monitoring-agent
      prompt: |
        Independently verify the cluster is healthy after remediation.
        Previous diagnosis was: {{ steps.diagnosis.output.summary }}
      output_key: verification
      condition: "steps.execution.output.success == true"
```

Key principles:
- **Diagnose before acting** — never go from problem to fix in one step
- **Structured output** — every agent returns a schema with confidence, risk, rollback plan
- **Human gate** — org policy decides what needs approval (auto-approve low-risk, require approval for high-risk)
- **Independent verify** — use a *different* agent to verify, not the one that did the fix
- **Ephemeral execution** — use `spawn: on-demand` so each step runs in an isolated sandbox (Phase 5)

---

## Examples in this repo

| Agent | YAML | Tools | Type |
|-------|------|-------|------|
| Diagnostic | `examples/agents/definitions/diagnostic-agent.yaml` | `examples/agents/tools/diagnostic_tools.py` | request-response, with output validator |
| Monitoring | `examples/agents/definitions/monitoring-agent.yaml` | `examples/agents/tools/monitoring_tools.py` | periodic-loop, dispatches to diagnostic |
| Deploy Readiness | `examples/agents/definitions/deploy-readiness-agent.yaml` | `examples/agents/tools/readiness_tools.py` | request-response, custom output type |
