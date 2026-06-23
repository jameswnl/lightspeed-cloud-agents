# Phase 3: Agent Workflow Executor — Design

**Date**: 2026-06-22
**Prerequisite**: Phase 2 (generic agent runtime template) complete
**Companion**: `cloud-agents.md` (Phase 3 section), `phase-2-template-design.md`

---

## Problem

Phase 2 gave us a generic agent runtime — one image, any agent type via mounted config. But each agent is still a **single-step execution**: prompt → tools → LLM → output.

Real-world use cases need **multi-step workflows**:
- RCA: diagnose → recommend → approve → execute → verify
- Upgrade: pre-check → plan → approve → execute → validate → report
- Incident: detect → triage → assign → remediate → post-mortem

Today, multi-step is either:
1. All in one agent's instructions (the LLM drives the steps — fragile for complex workflows)
2. Manual chaining via monitoring→diagnostic dispatch (only supports one hop)

**Goal**: A workflow executor that chains agent steps declaratively via YAML, with conditional branching, human approval gates, and state persistence.

---

## Design Principles

1. **Declarative** — workflows defined in YAML, not Python code
2. **Agent-native** — each step runs an existing agent (from the agent registry)
3. **Composable** — workflows can reference other workflows as steps
4. **Persistent** — workflow state survives pod restarts (via pydantic-graph `FileStatePersistence`)
5. **Observable** — each step produces structured output visible via polling
6. **Approval-gated** — human approval steps pause the workflow until approved

---

## Workflow Definition YAML

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentWorkflow
metadata:
  name: cluster-rca
  description: "4-step RCA workflow: diagnose → recommend → approve → execute"

spec:
  # Input passed to the first step
  input_prompt: "Investigate the cluster issue and remediate"

  steps:
    - name: diagnose
      type: agent
      agent: diagnostic-agent           # resolved via AgentRegistry
      prompt: "Diagnose all cluster issues. Report findings."
      output_key: diagnosis             # stored in workflow state

    - name: recommend
      type: agent
      agent: diagnostic-agent
      prompt: |
        Based on the diagnosis: {{ steps.diagnose.output.summary }}
        Issues found: {{ steps.diagnose.output.issues_found }}
        Recommend remediation actions.
      output_key: plan

    - name: approve
      type: human-approval
      message: |
        The diagnostic agent recommends these actions:
        {{ steps.recommend.output.summary }}
        Actions: {{ steps.recommend.output.actions_taken }}
        
        Approve to proceed with remediation.
      timeout_seconds: 1800            # 30 minutes
      output_key: approval

    - name: execute
      type: agent
      agent: diagnostic-agent
      condition: "steps.approve.approved == true"
      prompt: |
        Execute these remediation actions:
        {{ steps.recommend.output.summary }}
        Fix all issues and verify the fixes.
      output_key: execution

    - name: verify
      type: agent
      agent: deploy-readiness-agent
      condition: "steps.execute.output.cluster_healthy == true"
      prompt: "Verify the cluster is ready for normal operations."
      output_key: verification
```

---

## Workflow Definition Model

```python
class WorkflowStepSpec(BaseModel):
    """A single step in a workflow."""
    name: str
    type: Literal["agent", "human-approval"]
    agent: Optional[str] = None              # agent name (for type=agent)
    prompt: Optional[str] = None             # prompt template with {{ }} interpolation
    output_key: str                          # key in workflow state for this step's output
    condition: Optional[str] = None          # simple expression — skip step if false
    message: Optional[str] = None            # human-readable message (for type=human-approval)
    timeout_seconds: int = 3600              # timeout for the step

class WorkflowSpec(BaseModel):
    """Full workflow specification."""
    input_prompt: Optional[str] = None
    steps: list[WorkflowStepSpec]

class WorkflowDefinition(BaseModel):
    """Top-level workflow definition from workflow.yaml."""
    apiVersion: str
    kind: Literal["AgentWorkflow"]
    metadata: dict[str, Any]
    spec: WorkflowSpec
```

---

## Workflow State

```python
class StepResult(BaseModel):
    """Result of a single workflow step."""
    step_name: str
    status: Literal["pending", "running", "completed", "failed", "skipped", "awaiting_approval"]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

class WorkflowState(BaseModel):
    """Full state of a workflow execution."""
    workflow_id: str
    workflow_name: str
    status: Literal["running", "completed", "failed", "paused"]
    current_step: Optional[str] = None
    steps: dict[str, StepResult] = Field(default_factory=dict)
    created_at: str
    updated_at: str
```

---

## Workflow Executor

The executor is a new module in `src/agents/runtime/` that:

1. Reads a `WorkflowDefinition` from YAML
2. Iterates through steps sequentially
3. For each `agent` step: calls the agent via `RemoteAgentClient`, stores result in state
4. For each `human-approval` step: pauses the workflow, waits for approval via API
5. Evaluates `condition` expressions before each step — skips if false
6. Interpolates `{{ steps.X.output.Y }}` templates in prompts
7. Persists state after each step (via pydantic-graph `FileStatePersistence` or in-memory)

```python
class WorkflowExecutor:
    """Executes a multi-step agent workflow."""

    def __init__(
        self,
        definition: WorkflowDefinition,
        registry: AgentRegistry,
        persistence: Optional[StatePersistence] = None,
    ):
        ...

    async def run(self, input_prompt: str | None = None) -> WorkflowState:
        """Execute the workflow from start to completion."""
        ...

    async def resume(self, workflow_id: str, approval: bool = True) -> WorkflowState:
        """Resume a paused workflow after human approval."""
        ...

    async def get_state(self, workflow_id: str) -> WorkflowState:
        """Get current workflow state."""
        ...
```

### Step execution flow

```
for step in workflow.spec.steps:
    1. Check condition — skip if false
    2. Interpolate prompt template with prior step outputs
    3. If type == "agent":
       a. Resolve agent endpoint from registry
       b. Call agent via RemoteAgentClient.run()
       c. Store result in state[step.output_key]
    4. If type == "human-approval":
       a. Set status to "paused"
       b. Persist state
       c. Return — workflow resumes when resume() is called
    5. Persist state after each step
```

### Prompt template interpolation

`{{ }}` syntax, resolved from workflow state. Handles missing keys and nested values explicitly.

```python
import json
import re

TEMPLATE_PATTERN = re.compile(r"\{\{\s*steps\.(\w+)\.output\.(\w+)\s*\}\}")

def interpolate(template: str, state: WorkflowState) -> str:
    """Replace {{ steps.X.output.Y }} with values from workflow state.

    Rules:
    - Missing step or key → raises ValueError (fail fast, don't send broken prompts)
    - str values → inserted directly
    - dict/list values → JSON-serialized (not Python repr)
    - None → "null"
    """
    def replacer(match: re.Match) -> str:
        step_name, key = match.group(1), match.group(2)
        result = state.steps.get(step_name)
        if result is None or result.output is None:
            raise ValueError(f"Template references missing step '{step_name}'")
        value = result.output.get(key)
        if value is None:
            return "null"
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return TEMPLATE_PATTERN.sub(replacer, template)
```

**`input_from` is removed** — it was redundant with `{{ }}` templates. If a step needs prior output, it uses `{{ steps.X.output.Y }}` in its prompt. No implicit input passing.

### Condition evaluation

Restricted expression grammar — no `eval()`, no arbitrary code.

**Supported grammar:**
```
condition := expr
           | expr "and" expr
           | expr "or" expr

expr := path "==" value
      | path "!=" value
      | path                    # truthy check

path := "steps." step_name ".status"
      | "steps." step_name ".output." key
      | "steps." step_name ".approved"    # shorthand for human-approval steps

value := "true" | "false" | "null" | quoted_string | number
```

**Examples:**
```yaml
condition: "steps.approve.approved == true"
condition: "steps.execute.output.cluster_healthy == true"
condition: "steps.diagnose.status == completed"
condition: "steps.diagnose.output.issues_found"          # truthy — non-empty list
```

```python
import re

CONDITION_PATTERN = re.compile(
    r"steps\.(\w+)\.(status|approved|output\.(\w+))\s*(==|!=)?\s*(.*)?$"
)

def evaluate_condition(condition: str, state: WorkflowState) -> bool:
    """Evaluate a condition expression against workflow state.

    Raises ValueError on unparseable conditions.
    """
    # Handle "and" / "or" by splitting
    if " and " in condition:
        parts = condition.split(" and ")
        return all(evaluate_condition(p.strip(), state) for p in parts)
    if " or " in condition:
        parts = condition.split(" or ")
        return any(evaluate_condition(p.strip(), state) for p in parts)

    match = CONDITION_PATTERN.match(condition.strip())
    if not match:
        raise ValueError(f"Unparseable condition: {condition}")

    step_name = match.group(1)
    field_path = match.group(2)
    output_key = match.group(3)  # None if not output.X
    operator = match.group(4)     # None for truthy check
    raw_value = match.group(5)

    result = state.steps.get(step_name)
    if result is None:
        return False

    # Resolve the actual value
    if field_path == "status":
        actual = result.status
    elif field_path == "approved":
        actual = result.output.get("approved", False) if result.output else False
    elif output_key:
        actual = result.output.get(output_key) if result.output else None
    else:
        return False

    # Truthy check (no operator)
    if operator is None:
        return bool(actual)

    # Parse expected value
    expected: Any
    if raw_value.strip() == "true":
        expected = True
    elif raw_value.strip() == "false":
        expected = False
    elif raw_value.strip() == "null":
        expected = None
    else:
        expected = raw_value.strip().strip('"').strip("'")

    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    return False
```

**Human approval steps** set `output.approved = true/false` in their `StepResult.output`, so `steps.approve.approved == true` works naturally.

---

## Workflow API

The workflow executor exposes HTTP endpoints on a new **workflow runner** pod:

```
POST /v1/workflows/run
  body: { "workflow": "cluster-rca", "input_prompt": "..." }
  returns: 202 + { "workflow_id": "...", "status": "running" }

GET /v1/workflows/{workflow_id}
  returns: WorkflowState (current step, all step results)

POST /v1/workflows/{workflow_id}/approve
  body: { "approved": true }
  returns: WorkflowState (resumes from paused step)

GET /v1/workflows
  returns: list of active workflows
```

---

## Human Approval Flow

```
Step 1-2: Agent steps run automatically
    ↓
Step 3: type=human-approval
    ↓
Workflow pauses → state persisted
    ↓
API returns status: "paused", current_step: "approve"
    ↓
Human reviews step message + prior step outputs
    ↓
POST /v1/workflows/{id}/approve {"approved": true}
    ↓
Workflow resumes from step 4
    ↓
Steps 4-5 execute
    ↓
Workflow completes
```

If the human rejects (`approved: false`), the workflow marks the step as failed and stops.

If timeout expires, the workflow marks the approval step as `failed` with error "approval timed out".

---

## State Persistence

Two options, chosen at deploy time:

### In-memory (default)
- Simple `dict[str, WorkflowState]`
- Workflows lost on pod restart
- Good for dev/demo

### File-based (pydantic-graph)
- `FileStatePersistence` from pydantic-graph
- Workflows survive pod restart
- State written to `/app/state/` (volume mount)

### Database-backed (Phase 4)
- DBOS or PostgreSQL
- Production-grade durability
- Not in Phase 3 scope

---

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                  Workflow Runner Pod                     │
│                                                         │
│  /v1/workflows/run ──→ WorkflowExecutor                │
│  /v1/workflows/{id} ──→ get_state()                    │
│  /v1/workflows/{id}/approve ──→ resume()               │
│                                                         │
│  WorkflowExecutor                                       │
│    ├── reads workflow.yaml                              │
│    ├── iterates steps                                   │
│    ├── calls agents via RemoteAgentClient               │
│    ├── pauses on human-approval steps                   │
│    └── persists state after each step                   │
│                                                         │
│         ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│         │diagnostic│   │monitoring│   │readiness │    │
│         │  agent   │   │  agent   │   │  agent   │    │
│         └──────────┘   └──────────┘   └──────────┘    │
│              ▲              ▲              ▲           │
│              └──────────────┴──────────────┘           │
│                   via RemoteAgentClient                 │
└────────────────────────────────────────────────────────┘
```

The workflow runner is itself an agent pod running on `agent-runtime:latest`. Its "tools" are `RemoteAgentClient` calls to other agents.

---

## Deployment

```bash
# Same image — workflow runner is just another agent type
podman run -d --name workflow-runner \
  --network cloud-agents \
  -p 8084:8080 \
  -v $PWD/workflows/cluster-rca.yaml:/app/workflow.yaml:ro \
  -v $PWD/agents/registry.yaml:/app/registry.yaml:ro \
  -e WORKFLOW_MODE=true \
  agent-runtime:latest
```

Or a dedicated workflow entrypoint that loads `workflow.yaml` instead of `agent.yaml`.

---

## TDD Task Breakdown

| Task | What | Tests | Est. |
|------|------|-------|------|
| 1. WorkflowDefinition model | Pydantic model for workflow.yaml | YAML parsing, validation, step types | 1d |
| 2. WorkflowState model | State tracking for workflow execution | Status transitions, step results, serialization | 0.5d |
| 3. Prompt interpolation | `{{ steps.X.output.Y }}` template resolution | Happy path, missing keys, nested values | 0.5d |
| 4. Condition evaluator | Safe expression evaluation (no eval) | `== true`, `!= value`, missing step, type errors | 1d |
| 5. WorkflowExecutor | Core executor — iterate steps, call agents, handle conditions | Success path with mocked agents, skip on condition, error handling | 2d |
| 6. Human approval | Pause/resume mechanism with timeout | Pause stores state, resume continues, timeout fails, reject stops | 1.5d |
| 7. Workflow API | HTTP endpoints for run/poll/approve | FastAPI TestClient tests for all endpoints | 1d |
| 8. State persistence | File-based persistence for workflow state | Save/load round-trip, resume after restart | 1d |
| 9. Workflow runner entrypoint | Container entrypoint that loads workflow.yaml | Startup, healthz, missing file errors | 0.5d |
| 10. E2E | Full workflow across pods | Submit → agents run → approve → complete | 1d |

**Implementation order:** 1→2→3→4→5→6→7 (local code + tests) → 8→9→10 (persistence + containers + E2E)

**Estimated effort:** ~10 engineering days, ~12 with reviews.

---

## What's Deferred to Phase 4

- Database-backed state persistence (DBOS/PostgreSQL)
- Parallel step execution (all steps are sequential in Phase 3)
- Workflow-to-workflow composition (nested workflows)
- AI-generated workflows (Workflow Designer Agent)
- Approval via Slack/email (Phase 3 uses HTTP API only)
- Workflow versioning and rollback
- Workflow visualization (graph rendering)
- CRD-based workflow deployment (K8s operator)
- Retry policies per step (Phase 3 fails the workflow on step failure)
- On-demand agent pod spawning — create K8s Jobs or Podman containers per workflow step instead of pre-deployed agents. Via `spawn: on-demand` in step spec, using Kubernetes Python client (`BatchV1Api`) or Podman Python SDK (`podman-py`). Phase 3 uses pre-deployed agents only.

---

## Security

- **Condition expressions** use a restricted parser, not `eval()` — only `steps.X.Y == value` grammar
- **Prompt templates** interpolate values as strings via regex — no code injection possible
- **Approval endpoint** requires a shared secret token via `Authorization: Bearer <token>` header. The token is set via `WORKFLOW_APPROVAL_TOKEN` env var. This is minimum auth — not full RBAC, but prevents accidental or unauthorized approvals from any pod on the cluster network. Phase 4 adds proper auth integration.
- **Workflow state** may contain sensitive agent outputs — file persistence uses `0600` permissions on the state directory
- **Template interpolation** raises `ValueError` on missing keys — never sends broken prompts to agents

---

## Example: Full RCA Workflow

```
User: POST /v1/workflows/run
  {"workflow": "cluster-rca"}

Step 1 (diagnose):
  → diagnostic-agent: "Diagnose all cluster issues"
  ← DiagnosticReport: web-02 degraded, app crashed
  → state.steps.diagnose.output = {...}

Step 2 (recommend):
  → diagnostic-agent: "Based on diagnosis: web-02 degraded..."
  ← DiagnosticReport: recommend rollback + restart
  → state.steps.recommend.output = {...}

Step 3 (approve):
  → PAUSED. Waiting for human approval.
  → GET /v1/workflows/{id} → status: "paused", current_step: "approve"