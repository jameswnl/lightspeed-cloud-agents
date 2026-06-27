# Cloud Agents Framework — Temporal + Agentic Sandbox Architecture

**Date**: 2026-06-26
**Status**: Design proposal

## Overview

This document specifies the Cloud Agents Framework architecture using:

1. **Temporal** as the workflow orchestration engine
2. **lightspeed-agentic-sandbox** as the agent runtime for workflow step pods
3. **Pydantic AI agent-runtime** as an optional lightweight alternative

The sandbox is maintained by the OpenShift Lightspeed Agentic team. This architecture reuses it as shared infrastructure, avoiding the need to build and maintain a separate production-grade runtime image.

## Why the Sandbox

The sandbox (`lightspeed-agentic-sandbox`) is a FastAPI server that wraps LLM provider SDKs behind a single HTTP contract. It ships as a production-hardened container:

- **RHEL 9 base**, Konflux-built, CVE-scanned
- **Multi-provider**: Claude Code (`claude-agent-sdk`), OpenAI agents (`openai-agents`), Gemini (`google-adk`)
- **Cluster tools**: kubectl, oc, git, ripgrep, jq, bash
- **Security**: non-root (UID 1001), read-only rootfs, drop ALL capabilities
- **Skills**: loaded from `/app/skills` (OCI image volumes)
- **Structured output**: JSON Schema enforcement via SDK-native mechanisms

Building an equivalent production-ready runtime from scratch would duplicate months of hardening work.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Customer Cluster (OCP / Podman)                                     │
│                                                                      │
│  ┌──────────────────────┐      ┌─────────────────────────────────┐   │
│  │  FastAPI Service      │      │  Temporal Server                │   │
│  │  (Temporal Client)    │─────▶│  K8s: Helm chart                │   │
│  │                       │      │  Podman: podman-compose          │   │
│  │  POST /workflows/run  │      │  + PostgreSQL                   │   │
│  │  POST /workflows/:id/ │      └──────────┬──────────────────────┘   │
│  │       approve         │                 │                          │
│  │  GET  /workflows/:id/ │                 │  task dispatch           │
│  │       events (SSE)    │                 ▼                          │
│  └──────────────────────┘      ┌─────────────────────────────────┐   │
│                                │  Temporal Workers                │   │
│                                │                                  │   │
│                                │  AgentWorkflow (@workflow.defn)  │   │
│                                │  ├─ step loop (YAML-driven)     │   │
│                                │  ├─ conditions + parallel       │   │
│                                │  ├─ approval signals            │   │
│                                │  └─ retry + escalation          │   │
│                                │                                  │   │
│                                │  Activities:                     │   │
│                                │  ├─ run_sandbox_step()           │   │
│                                │  ├─ run_generic_step()           │   │
│                                │  └─ build_escalation()           │   │
│                                └──────────┬───────────────────────┘   │
│                                           │                           │
│                                ┌──────────▼───────────────────────┐   │
│                                │  Sandbox Pods (per step)          │   │
│                                │  lightspeed-agentic-sandbox:tag   │   │
│                                │                                   │   │
│                                │  POST /v1/agent/run               │   │
│                                │  ├─ Claude Code / OpenAI / Gemini │   │
│                                │  ├─ kubectl, oc, bash, git        │   │
│                                │  ├─ /app/skills/ (OCI volumes)    │   │
│                                │  └─ MCP servers (planned)         │   │
│                                └───────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

## Lightspeed-Stack Integration

The Cloud Agents framework runs **inside** lightspeed-stack, not beside it. It reuses the stack's shared infrastructure rather than rebuilding auth, database, config, observability, and middleware.

### Auth: Reuse, don't rebuild

The stack has a mature auth system with pluggable backends:

| Stack module | What it provides | How Cloud Agents uses it |
|---|---|---|
| `K8SAuthDependency` | K8s TokenReview + SubjectAccessReview | Validates bearer tokens on workflow API endpoints. Same auth that protects `/v1/query`. |
| `JwkTokenAuthDependency` | JWT validation with JWK caching | Alternative auth for non-K8s deployments (Podman with external IdP). |
| `NoopAuthDependency` | Passthrough for dev/testing | Used in local development without auth overhead. |
| `@authorize(action)` decorator | Action-level RBAC enforcement | Protects workflow trigger/approve/view endpoints with role-based access. |
| `get_auth_dependency()` factory | Selects auth module from config | Cloud Agents gets the same auth backend as the rest of the stack — no separate config. |

**Integration point**: Workflow API routers use the stack's `auth_dependency` as a FastAPI dependency, exactly like existing endpoints:

```python
from authentication import get_auth_dependency

router = APIRouter(prefix="/v1/workflows")

@router.post("/run")
async def run_workflow(
    request: WorkflowRunRequest,
    auth: AuthTuple = Depends(get_auth_dependency()),
):
    user_id, user_name, _, token = auth
    # user_id and token available for RBAC checks
```

This means:
- On OCP: K8s TokenReview validates the bearer token, SAR checks authorization — same as `/v1/query`
- On Podman with JWT: JWK token validation + role extraction — same as the rest of the API
- In dev: NoopAuth passes through — no auth setup needed
- **No separate auth system for Cloud Agents**

### Database: Shared engine, separate tables

The stack initializes a SQLAlchemy engine (SQLite or PostgreSQL) at startup via `initialize_database()`. Cloud Agents extends the same database with workflow-specific tables:

```python
from models.database.base import Base

class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(String, primary_key=True)
    workflow_id = Column(String, index=True)
    step_name = Column(String)
    status = Column(String)
    output = Column(JSON)
    created_at = Column(DateTime, default=func.now())

class ApprovalLog(Base):
    __tablename__ = "approval_logs"
    id = Column(String, primary_key=True)
    workflow_id = Column(String, index=True)
    step_name = Column(String)
    decision = Column(String)
    approver = Column(String)
    decided_at = Column(DateTime)
```

**Note**: With Temporal, workflow state itself lives in Temporal Server's database. These tables are for **audit logs and queryable history** — not for workflow execution state. The stack's database provides a durable record of who triggered what, who approved what, and what the outcomes were.

### Configuration: Extend AppConfig

The stack loads config from YAML via `AppConfig` singleton. Cloud Agents adds its sections:

```yaml
# lightspeed-stack config YAML
agents:
  temporal:
    server_address: "temporal:7233"
    task_queue: "agents"
    namespace: "default"
  sandbox:
    image: "lightspeed-agentic-sandbox:latest"
    skills_image: "quay.io/lightspeed/agentic-skills:latest"
  provider:
    name: "openai"
    model: "gpt-5.5"
    credentials_secret: "openai-key"
  approval:
    auto_approve_risk_levels: ["low"]
    default_timeout_seconds: 86400
```

Accessed via the existing config pattern:

```python
from configuration import configuration

temporal_addr = configuration.agents.temporal.server_address
sandbox_image = configuration.agents.sandbox.image
```

### Observability: Use existing patterns

| Stack facility | Cloud Agents usage |
|---|---|
| `get_logger(__name__)` | All agent/workflow logging |
| `RestApiMetricsMiddleware` | Prometheus metrics on workflow API endpoints |
| Custom Prometheus counters | `agent_runs_total`, `agent_step_duration_seconds`, `workflow_approvals_total` |
| `send_splunk_event()` | Workflow completion and escalation events |
| OTel `get_tracer()` | Distributed traces across API → Temporal → sandbox pod |

### FastAPI: Register as routers

Cloud Agents endpoints are registered alongside existing routers in `include_routers()`:

```python
# In src/app/routers.py
from agents.workflow.temporal_api import workflow_router

def include_routers(app: FastAPI):
    # ... existing routers ...
    app.include_router(workflow_router)  # /v1/workflows/*
```

The workflow router uses the same middleware stack (CORS, metrics, exception handling, auth) as all other endpoints.

### MCP: Shared server registry

MCP servers registered via the stack's config or API (`/v1/mcp-servers`) are available to Cloud Agents workflows. The `LIGHTSPEED_MCP_SERVERS` env var injected into sandbox pods is built from the same registry:

```python
mcp_servers = configuration.mcp_servers  # shared with conversation endpoints
sandbox_env["LIGHTSPEED_MCP_SERVERS"] = json.dumps([
    {"name": s.name, "url": s.url, "headers": resolve_headers(s)}
    for s in mcp_servers
])
```

### Quota: Apply to agent runs

The stack's `QuotaLimiter` can enforce per-user or per-cluster limits on workflow runs:

```python
from quota import QuotaLimiterFactory

limiter = QuotaLimiterFactory.create(configuration.quota_handlers)

@router.post("/run")
async def run_workflow(request, auth):
    user_id = auth[0]
    if not limiter.check(user_id, tokens_requested=1):
        raise HTTPException(429, "Workflow quota exceeded")
    # ... start workflow
```

### What this means for deployment

Cloud Agents is NOT a separate service. It's a set of routers and a Temporal worker running inside the existing lightspeed-stack deployment:

```
┌─────────────────────────────────────────────────────┐
│  lightspeed-stack (single deployment)               │
│                                                     │
│  FastAPI app                                        │
│  ├─ /v1/query          (existing: conversations)    │
│  ├─ /v1/responses      (existing: OpenAI-compat)    │
│  ├─ /v1/mcp-servers    (existing: MCP management)   │
│  ├─ /v1/workflows/run  (NEW: Cloud Agents)          │
│  ├─ /v1/workflows/:id  (NEW: Cloud Agents)          │
│  └─ /metrics, /health  (existing)                   │
│                                                     │
│  Shared infrastructure                              │
│  ├─ Auth (K8s/JWT/Noop)                             │
│  ├─ Database (SQLite/PostgreSQL)                    │
│  ├─ Config (YAML → AppConfig)                       │
│  ├─ Observability (OTel, Prometheus, Splunk)         │
│  └─ MCP server registry                             │
│                                                     │
│  Temporal worker (in-process or sidecar)            │
│  ├─ AgentWorkflow                                   │
│  └─ Activities (spawn, call sandbox, escalation)    │
└─────────────────────────────────────────────────────┘
         │
         ▼
    Temporal Server (separate deployment)
         │
         ▼
    Sandbox pods (ephemeral, per step)
```

## Interfaces

### 1. Orchestrator → Sandbox: HTTP Contract

The Temporal activity calls the sandbox at `POST /v1/agent/run`.

**Request:**
```json
{
  "query": "...",
  "systemPrompt": "You are a diagnostic agent. Analyze the problem...",
  "outputSchema": { ... },
  "context": {
    "targetNamespaces": ["production"],
    "previousAttempts": [{"attempt": 1, "failureReason": "..."}],
    "approvedOption": { ... },
    "executionResult": { ... }
  },
  "timeout_ms": 300000
}
```

| Field | Purpose | Source |
|---|---|---|
| `query` | Step prompt (interpolated with prior step outputs) | Workflow YAML `step.prompt` |
| `systemPrompt` | Agent role instructions | Agent definition YAML `spec.instructions` |
| `outputSchema` | JSON Schema for structured output | Agent definition YAML `spec.output_schema` |
| `context` | Accumulated workflow state from prior steps | Temporal workflow `self._steps` |
| `timeout_ms` | Hard timeout for this step | Workflow YAML `step.timeout_seconds * 1000` |

**Response:**
```json
{
  "success": true,
  "summary": "Root cause: OOMKilled due to 256Mi limit",
  "risk_level": "medium",
  "actions": [{"type": "patch", "description": "Increase to 512Mi"}],
  "rollback_plan": {"description": "Revert memory limit", "command": "kubectl ..."}
}
```

The response shape is determined by `outputSchema`. The `success` and `summary` fields are always present. Additional fields (e.g., `risk_level`, `actions`) are enforced by the SDK's structured output mechanism and passed through via `extra="allow"` on the response model.

### 2. Orchestrator → Sandbox: Pod Configuration

The Temporal activity configures each sandbox pod via env vars and volume mounts, following the operator's generic env var contract [^spec16a] — using `LIGHTSPEED_MODEL` and `LIGHTSPEED_PROVIDER` instead of SDK-specific names like `ANTHROPIC_MODEL`:

[^spec16a]: "Spec 16a" refers to rule 16a in the operator's internal specification at `lightspeed-agentic-operator/.ai/spec/what/sandbox-execution.md`. Rule 16 covers LLM credential mounting; rule 16a defines the generic `LIGHTSPEED_*` env var contract between the operator and the sandbox. These rules are referenced throughout this document for traceability to the upstream design.

**Env vars (set by the spawner):**

| Env var | Required | Value source |
|---|---|---|
| `LIGHTSPEED_AGENT_PROVIDER` | Yes | Mapped from workflow config: `claude`, `openai`, `gemini` |
| `LIGHTSPEED_MODEL` | Yes | From agent definition or workflow step override |
| `LIGHTSPEED_SKILLS_DIR` | No | Default `/app/skills` |
| `LIGHTSPEED_MODE` | No | Step type: `analysis`, `execution`, `verification` (currently unused by sandbox but set for forward compat) |
| `LIGHTSPEED_MCP_SERVERS` | When MCP configured | JSON array of MCP server configs |

**Credentials (set by spawner via K8s SecretKeyRef or Podman host env):**

| Provider | Expected env vars |
|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY` |
| Claude (Vertex) | `GOOGLE_APPLICATION_CREDENTIALS` (file path) |
| OpenAI | `OPENAI_API_KEY`, optionally `OPENAI_BASE_URL` |
| Gemini | `GOOGLE_API_KEY` or ADC via `GOOGLE_APPLICATION_CREDENTIALS` |

Credentials are loaded via `envFrom: secretRef` (K8s) or host env propagation (Podman). Never passed as literal env vars in the pod spec.

**Volume mounts:**

| Mount | Content | Source |
|---|---|---|
| `/app/skills/` | Domain-specific skill packages | OCI image volume from skills image |
| `/var/run/secrets/llm-credentials/` | LLM provider credentials (file form) | K8s Secret volume or Podman bind mount |

### 3. Workflow YAML → Temporal Workflow

A single generic `AgentWorkflow` class interprets any workflow YAML at runtime:

```yaml
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: diagnose-and-fix
spec:
  # Provider config — applies to all steps unless overridden
  provider:
    name: openai           # claude | openai | gemini
    model: gpt-5.5
    credentials_secret: openai-key  # K8s Secret name or Podman env var name
  
  # Skills image — mounted on all sandbox pods
  skills:
    image: quay.io/my-team/my-skills:latest
    paths: [/skills/diagnostics, /skills/remediation]

  steps:
    - name: diagnose
      type: agent
      runtime: sandbox            # sandbox | generic
      instructions: |
        You are a cluster diagnostic agent. Investigate the problem,
        identify root cause, and propose remediation options.
      output_schema:
        type: object
        properties:
          summary: { type: string }
          risk_level: { type: string, enum: [low, medium, high, critical] }
          root_cause: { type: string }
          options:
            type: array
            items:
              type: object
              properties:
                title: { type: string }
                actions: { type: array, items: { type: object } }
                rollback: { type: string }
              required: [title, actions]
        required: [summary, risk_level, root_cause, options]
      prompt: "Investigate: {{ input_prompt }}"
      output_key: diagnosis
      risk_level: low
      spawn: ephemeral
      timeout_seconds: 300

    - name: approve-fix
      type: human-approval
      message: |
        Diagnosis: {{ steps.diagnosis.output.summary }}
        Risk: {{ steps.diagnosis.output.risk_level }}
        Proposed fix: {{ steps.diagnosis.output.options[0].title }}
      output_key: approval

    - name: fix
      type: agent
      runtime: sandbox
      instructions: |
        You are an execution agent. Execute the approved remediation
        option exactly as specified. Do not re-analyze.
      output_schema:
        type: object
        properties:
          success: { type: boolean }
          actions_taken: { type: array, items: { type: object } }
        required: [success, actions_taken]
      prompt: |
        Execute: {{ steps.diagnosis.output.options[0].title }}
        Actions: {{ steps.diagnosis.output.options[0].actions }}
      output_key: fix
      risk_level: high
      spawn: ephemeral
      condition: "steps.approval.output.approved == true"
      timeout_seconds: 600
      max_retries: 2

    - name: verify
      type: agent
      runtime: sandbox
      instructions: |
        You are a verification agent. Verify the fix was applied
        correctly and the issue is resolved.
      output_schema:
        type: object
        properties:
          success: { type: boolean }
          checks: { type: array, items: { type: object } }
        required: [success, checks]
      prompt: |
        Verify that the following actions resolved the issue:
        {{ steps.fix.output.actions_taken }}
      output_key: verification
      risk_level: low
      spawn: ephemeral
      condition: "steps.fix.output.success == true"
      timeout_seconds: 120
```

**Key difference from the operator**: instructions and output schemas are in the workflow YAML, not hardcoded in Go templates. Product teams define their own agent behavior without touching framework code.

### 4. Agent Definition (Optional)

For reusable agents referenced across multiple workflows, agent definitions can be stored separately:

```yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: cluster-diagnostic
spec:
  instructions: |
    You are a cluster diagnostic agent...
  output_schema:
    type: object
    properties:
      summary: { type: string }
      risk_level: { type: string }
    required: [summary, risk_level]
```

Workflow steps can reference by name: `agent: cluster-diagnostic` — the orchestrator resolves instructions and output_schema from the definition store.

## Temporal Workflow Implementation

### Generic Workflow Class

```python
@workflow.defn
class AgentWorkflow:
    """Interprets any workflow YAML at runtime. Registered once at worker
    startup — new workflow definitions don't require worker restarts."""

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
        for group in group_steps_by_parallel(input.definition.steps):
            if len(group) == 1:
                result = await self._execute_step(group[0], input)
                if result and result.status in ("failed", "denied"):
                    break
            else:
                results = await asyncio.gather(*[
                    self._execute_step(s, input) for s in group
                ])
                if any(r.status == "failed" for r in results if r):
                    break
        return WorkflowOutput(steps=self._steps)
```

### Sandbox Activity

```python
@activity.defn
async def run_sandbox_step(input: SandboxStepInput) -> StepResult:
    """Spawn a sandbox pod, call POST /v1/agent/run, return structured result.

    Retry model:
    - Infrastructure failures (spawn fail, HTTP timeout, pod crash) propagate
      as exceptions → Temporal retries per RetryPolicy.
    - Application failures (agent ran but reported failure in output) return
      StepResult(status="failed") → not retried by Temporal, handled by workflow.
    """
    spawner = get_spawner()
    step = input.step

    pod_name = compute_spawn_name(
        input.workflow_id, step.name, activity.info().attempt
    )

    # Build sandbox pod env
    env = {
        "LIGHTSPEED_AGENT_PROVIDER": input.provider.name,
        "LIGHTSPEED_MODE": step.name,
    }
    secret_refs = {
        credential_env_var(input.provider): SecretKeyRef(
            secret_name=input.provider.credentials_secret,
            key=credential_key(input.provider),
        ),
    }

    endpoint = await spawner.spawn(
        name=pod_name,
        image=input.sandbox_image,
        env=env,
        secret_env_vars=secret_refs,
        skills_image=input.skills_image,
        skills_paths=input.skills_paths,
        config=step.spawn_config,
    )
    try:
        await spawner.wait_ready(endpoint, path="/health")

        # Build the sandbox HTTP request
        prompt = interpolate(step.prompt, input.context)
        request_body = {
            "query": prompt,
            "systemPrompt": step.instructions,
            "outputSchema": step.output_schema,
            "context": build_sandbox_context(input.context, step),
            "timeout_ms": (step.timeout_seconds or 600) * 1000,
        }

        client = RemoteAgentClient(
            endpoint=f"{endpoint}/v1/agent",
            auth_token=get_api_token(),
        )
        response = await client.post("/run", json=request_body)

        if not response.get("success", False):
            return StepResult(
                status="failed",
                error=response.get("summary", "agent reported failure"),
                output=response,
            )
        return StepResult(status="completed", output=response)

    # Infrastructure errors propagate → Temporal retries
    finally:
        await spawner.destroy(pod_name)
```

### Context Building

The sandbox expects a `context` dict with specific fields. The Temporal activity builds this from accumulated workflow state:

```python
def build_sandbox_context(
    workflow_steps: dict[str, StepResult],
    current_step: StepSpec,
    step_roles: dict[str, str],  # output_key → role (analysis|execution|verification)
) -> dict:
    """Build the sandbox context dict from accumulated workflow state.

    The sandbox's _format_context_prefix expects specific field shapes:
    - approvedOption: must have nested diagnosis.rootCause, proposal.description,
      proposal.risk, proposal.reversible, proposal.actions[].{type, description}
    - executionResult: {success, actionsTaken[]}
    - previousAttempts: [{attempt, failureReason}]
    - targetNamespaces: [str]

    Step roles are declared in the workflow YAML (step.role field) to avoid
    hardcoding step names. The role determines which context fields are built.
    """
    ctx = {}
    if current_step.target_namespaces:
        ctx["targetNamespaces"] = current_step.target_namespaces

    # Pass prior step failures for retry context
    previous = []
    for key, result in workflow_steps.items():
        if result.status == "failed":
            previous.append({
                "attempt": len(previous) + 1,
                "failureReason": result.error or "unknown",
            })
    if previous:
        ctx["previousAttempts"] = previous

    # Find the analysis step by role, not name
    for key, role in step_roles.items():
        if role == "analysis" and key in workflow_steps:
            output = workflow_steps[key].output
            if output and output.get("options"):
                # Pass the full option object — the output_schema must match
                # the sandbox's expected approvedOption shape (with nested
                # diagnosis, proposal, verification, rbac sub-objects)
                ctx["approvedOption"] = output["options"][0]

        if role == "execution" and key in workflow_steps:
            output = workflow_steps[key].output
            if output:
                ctx["executionResult"] = {
                    "success": output.get("success", False),
                    "actionsTaken": output.get("actions_taken", []),
                }

    return ctx
```

**Important: output schema alignment.** When the workflow builds context for an execution step, the `approvedOption` passed to the sandbox must match the structure that `_format_context_prefix` expects. This means the analysis step's `output_schema` should produce options with nested `diagnosis`, `proposal`, and `rbac` sub-objects — matching the operator's `AnalysisOutputSchema` from `controller/proposal/schemas.go`. The workflow YAML author is responsible for defining output schemas that produce sandbox-compatible structures. We provide reference schemas (see "Reference Output Schemas" section below).

### Step Roles

Steps declare their role in the workflow to enable type-safe context building without hardcoded names:

```yaml
steps:
  - name: investigate
    role: analysis         # identifies this step's output as analysis context
    # ...
  - name: remediate
    role: execution        # identifies this step's output as execution context
    # ...
  - name: check
    role: verification     # identifies this step's output as verification context
    # ...
```

The `role` field is optional. Steps without a role don't contribute to the sandbox context — their output is still available via `{{ steps.<output_key>.output }}` template interpolation, but not formatted into the sandbox's structured context prefix.

### Reference Output Schemas

To ensure output compatibility with the sandbox's `_format_context_prefix`, we provide reference schemas that match the operator's expected structure. These are recommended starting points — teams can extend them with additional fields.

**Analysis step reference schema** (produces `approvedOption`-compatible output):
```json
{
  "type": "object",
  "properties": {
    "options": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "title": { "type": "string" },
          "diagnosis": {
            "type": "object",
            "properties": {
              "summary": { "type": "string" },
              "rootCause": { "type": "string" },
              "confidence": { "type": "string" }
            },
            "required": ["summary", "rootCause"]
          },
          "proposal": {
            "type": "object",
            "properties": {
              "description": { "type": "string" },
              "actions": { "type": "array", "items": { "type": "object" } },
              "risk": { "type": "string" },
              "reversible": { "type": "string" }
            },
            "required": ["description", "actions", "risk"]
          }
        },
        "required": ["title", "diagnosis", "proposal"]
      }
    }
  },
  "required": ["options"]
}
```

When the workflow includes execution or verification steps, add `rbac` and `verification` sub-objects to each option (matching the operator's `AnalysisOutputSchema`):

```json
"rbac": {
  "type": "object",
  "properties": {
    "namespaceScoped": { "type": "array", "items": { "type": "object" } },
    "clusterScoped": { "type": "array", "items": { "type": "object" } }
  }
},
"verification": {
  "type": "object",
  "properties": {
    "description": { "type": "string" },
    "steps": { "type": "array", "items": { "type": "object" } }
  }
}
```

Teams that don't need the sandbox's context formatting can use any output schema — the context fields are only built when a step has a declared `role`.

## Sandbox Adaptations Required

### Already done upstream (verified 2026-06-26)

The following adaptations have been implemented in the sandbox since the initial design:

#### ~~1. Health endpoint~~ — Done
Sandbox exposes `GET /health` (liveness) and `GET /ready` (readiness with provider credential + endpoint checks). The spawner probes `/health`. No change needed.

#### ~~2. `LIGHTSPEED_MODEL` env var~~ — Done
`config.py:resolve_sdk()` reads `LIGHTSPEED_MODEL` and maps it to the SDK-specific env var. Resolution chain: `LIGHTSPEED_MODEL` → SDK-specific var → `DEFAULT_MODEL`.

#### ~~3. `LIGHTSPEED_PROVIDER` env var~~ — Done
`config.py:resolve_sdk()` reads `LIGHTSPEED_PROVIDER` and maps to SDK backend (`anthropic`→claude, `vertex`→claude/gemini/openai per `LIGHTSPEED_MODEL_PROVIDER`, `openai`→openai, `azure`→openai, `bedrock`→claude). Full spec 16a contract implemented including `LIGHTSPEED_PROVIDER_URL`, `_PROJECT`, `_REGION`, `_API_VERSION`.

#### ~~4. Credential volume mount~~ — Done
Credentials mounted at `/var/run/secrets/llm-credentials/` (configurable via `LIGHTSPEED_LLM_CREDENTIALS_PATH`). SDK-specific env vars (e.g., `GOOGLE_APPLICATION_CREDENTIALS`) point to files in this mount.

### Required (sandbox PRs needed)

#### 5. Handle `executionResult` in context formatting

**Problem**: `_format_context_prefix()` in `query.py` handles `targetNamespaces`, `previousAttempts`, and `approvedOption` — but NOT `executionResult`. Verification steps pass `context.executionResult` with `{success, actionsTaken}` but it's silently dropped. The verification agent doesn't see what was executed.

**Change**: Add `executionResult` handling to `_format_context_prefix()`:

```python
if exec_result := context.get("executionResult"):
    lines.append("")
    lines.append("=== EXECUTION RESULT ===")
    lines.append(f"Success: {exec_result.get('success', 'unknown')}")
    if actions := exec_result.get("actionsTaken"):
        lines.append("Actions taken:")
        for action in actions:
            outcome = action.get("outcome", "unknown")
            lines.append(f"  - [{action.get('type', '?')}] {action.get('description', '?')} → {outcome}")
    lines.append("=== END EXECUTION RESULT ===")
    lines.append("")
```

**Effort**: ~15 lines. Backward compatible — existing callers that don't send `executionResult` are unaffected.

#### 6. Return HTTP 502 for infrastructure errors

**Problem**: The sandbox catches all exceptions (including LLM provider errors, timeouts, DNS failures) and returns HTTP 200 with `success=false`. This makes it impossible for the Temporal activity to distinguish retriable infrastructure errors from non-retriable application failures.

**Change**: Classify errors before returning:

```python
except TimeoutError:
    return RunResponse(success=False, summary=f"Agent timed out after {timeout}ms")
except Exception as e:
    error_msg = str(e)
    # Provider/infrastructure errors → HTTP 502 so orchestrator can retry
    if _is_infrastructure_error(e):
        return JSONResponse(
            status_code=502,
            content={"success": False, "summary": f"Infrastructure error: {error_msg}"},
        )
    # Application-level agent errors → HTTP 200 with success=false
    return RunResponse(success=False, summary=f"Agent error: {error_msg}")
```

Where `_is_infrastructure_error()` checks exception types (not string matching):

```python
def _is_infrastructure_error(e: Exception) -> bool:
    """Check if the error is from infrastructure (retriable) vs application (not retriable)."""
    import httpx
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    retriable_types = (
        ConnectionError, TimeoutError, OSError,
        httpx.ConnectError, httpx.TimeoutException,
        APIConnectionError, APITimeoutError, RateLimitError,
    )
    return isinstance(e, retriable_types)
```

**Effort**: ~25 lines. Backward compatible — callers that don't check HTTP status codes still get `success=false` in the body.

This replaces the string-matching heuristic in the Temporal activity. The activity becomes:

```python
if response.status_code == 502:
    raise RuntimeError(f"Sandbox infrastructure error: {response.text}")
# 200 → parse as StepResult (success or application failure)
```

### High Priority (shared deliverable with Lightspeed Agentic team)

#### 4. MCP server support

**Current**: `LIGHTSPEED_MCP_SERVERS` env var is set by the operator's template derivation code, but the sandbox doesn't read it. Gemini provider has a TODO comment.
**Need**: Agents should be able to call external tool servers (ServiceNow, PagerDuty, Jira, internal APIs) via MCP protocol without changing the sandbox image.
**Change**:
- Read `LIGHTSPEED_MCP_SERVERS` env var (JSON array of `{name, url, headers}`)
- Create MCP toolsets per provider:
  - Claude: `claude-agent-sdk` MCP support
  - OpenAI: MCP toolset via `openai-agents` SDK
  - Gemini: `google-adk` MCP integration (TODO already noted)
- Pass MCP tools to the agent alongside default tools

**Effort**: 1-2 weeks. This is the highest-value shared investment — it makes the sandbox extensible without image changes.

### Required (Cloud Agents framework, not sandbox changes)

#### 5. Sandbox error classification in the activity

**Problem**: The sandbox returns HTTP 200 with `success=false` for both infrastructure errors (LLM API down, SDK crash) and application failures (agent ran but reported failure). The Temporal retry model depends on distinguishing these — infrastructure errors should propagate as exceptions for Temporal retry, application failures should not retry.

**Solution**: The `run_sandbox_step` activity classifies errors by inspecting the response:

```python
response = await client.post("/run", json=request_body)

if response.status_code != 200:
    # HTTP-level failure — infrastructure error, let Temporal retry
    raise RuntimeError(f"Sandbox HTTP {response.status_code}")

data = response.json()
if not data.get("success", False):
    error_msg = data.get("summary", "")
    # Classify: infrastructure errors contain SDK/provider error patterns
    if any(pattern in error_msg for pattern in [
        "Error code:", "timeout", "connection refused", "DNS",
    ]):
        raise RuntimeError(f"Sandbox infrastructure error: {error_msg}")
    # Application-level failure — agent ran but reported failure
    return StepResult(status="failed", error=error_msg, output=data)
```

This is imperfect — the heuristic may misclassify some errors. A better long-term solution is for the sandbox to return distinct HTTP status codes (e.g., 502 for provider errors, 200 for agent results). This is captured as a future sandbox adaptation.

#### 6. Condition evaluation engine

**Problem**: Workflow YAML uses conditions like `steps.approval.output.approved == true`. The evaluation mechanism must be safe (no `eval()`), support nested field access, and be deterministic (Temporal requirement).

**Solution**: Reuse the existing `evaluate_condition()` from `src/agents/workflow/conditions.py` — it's already a safe regex-based parser with no `eval()`, supporting `==`, `!=`, `and`, `or` operators with `steps.X.output.Y` references. This function is pure (no I/O, no randomness) and safe for Temporal workflow code.

Template interpolation (`{{ steps.X.output.Y }}`) uses Jinja2's `SandboxedEnvironment` which prevents code execution. Both are existing, tested components from the Cloud Agents framework.

#### 7. Per-step RBAC scoping

**Problem**: All sandbox pods run with the same ServiceAccount. A diagnosis step (read-only) gets the same cluster RBAC as an execution step (read-write).

**Solution**: Workflow steps declare a `service_account` in their permissions:

```yaml
steps:
  - name: diagnose
    role: analysis
    permissions:
      service_account: diagnostic-readonly  # read-only SA
    # ...
  - name: fix
    role: execution
    permissions:
      service_account: remediation-exec     # write-capable SA
    # ...
```

The spawner creates the K8s Job with the specified ServiceAccount. For Podman, RBAC is not applicable — the container runs with host-level permissions as documented.

The framework does NOT dynamically create ServiceAccounts or Roles (unlike the operator). Product teams pre-create their RBAC resources as part of deployment. The framework uses them.

**Future enhancement**: Dynamic RBAC creation from agent output (like the operator's `ensureExecutionRBAC`). This requires a separate design and is deferred.

#### 8. Parallel step specification

Steps declare parallel execution via `parallel_group`:

```yaml
steps:
  - name: check-pods
    parallel_group: diagnostics
    # ...
  - name: check-nodes
    parallel_group: diagnostics
    # ...
  - name: summarize
    # no parallel_group — runs after the group completes
    condition: "steps.check-pods.status == completed"
    # ...
```

Steps with the same `parallel_group` value run concurrently via `asyncio.gather()` in the Temporal workflow. Steps without `parallel_group` run sequentially. Groups execute in YAML order — all steps in a group must complete before the next sequential step or group begins.

### Required (Cloud Agents framework, no sandbox changes)

#### 7. Workflow cancellation endpoint

Users must be able to stop a running workflow. Without this, a mistakenly triggered high-risk workflow has no abort mechanism.

```python
@router.post("/workflows/{id}/cancel")
async def cancel_workflow(id: str, auth: AuthTuple = Depends(get_auth_dependency())):
    handle = temporal_client.get_workflow_handle(id)
    await handle.cancel()
    return {"status": "cancelled"}
```

Temporal supports cancellation natively — the workflow receives a `CancelledError` and the `finally` block in the activity cleans up the sandbox pod.

#### 8. Temporal worker runs as a sidecar container

**Decision: Sidecar, not in-process.**

The Temporal worker runs as a separate container in the same pod (K8s) or a separate container on the same network (Podman). Rationale:
- **Isolation**: A sandbox spawn that blocks (waiting for pod readiness) doesn't affect FastAPI API latency
- **Independent scaling**: Workers can be scaled separately from the API
- **Resource limits**: Worker memory/CPU limits are set independently (recommended: 512Mi / 500m per worker, `max_concurrent_activities=10`)
- **Failure isolation**: Worker crash doesn't take down the API; API crash doesn't stop in-flight workflows

The FastAPI service is the Temporal **client** (starts workflows, sends signals, queries state). The sidecar is the Temporal **worker** (executes workflow code and activities).

#### 9. Minimum Kubernetes version: 1.31+

OCI image volumes (used for skills mounting) require the `ImageVolume` feature gate, which is beta in K8s 1.31 and GA in 1.33. OpenShift 4.18+ includes K8s 1.31.

For clusters on older K8s versions, the spawner falls back to an init-container pattern that copies skills from the image to an emptyDir volume:

```python
if k8s_version < (1, 31):
    # Init container copies skills to shared emptyDir
    init_container = V1Container(
        name="skills-loader",
        image=skills_image,
        command=["cp", "-r", "/skills/.", "/shared-skills/"],
        volume_mounts=[V1VolumeMount(name="skills", mount_path="/shared-skills")],
    )
    volumes.append(V1Volume(name="skills", empty_dir=V1EmptyDirVolumeSource()))
else:
    # Native OCI image volume (K8s 1.31+)
    volumes.append({"name": "skills", "image": {"reference": skills_image}})
```

### Nice to Have (future)

#### 5. Advisory mode / tool filtering

**Current**: sandbox has no concept of read-only vs write tools
**Use case**: advisory-only workflow runs where the agent should diagnose but not execute
**Approach**: Read an env var (e.g., `LIGHTSPEED_ADVISORY=true`) and filter tools to read-only subset. Alternatively, pass `allowed_tools` in the request context.
**Effort**: 1 week. Lower priority — can be enforced at the RBAC level (read-only ServiceAccount on the pod) instead of tool filtering.

#### 6. Request-level tool configuration

**Current**: `DEFAULT_ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep", "Skill"]` is hardcoded
**Use case**: Per-step tool scoping from workflow YAML
**Approach**: Accept `allowed_tools` in the request body or as an env var
**Effort**: 2-3 days. Low priority — current default tools cover most cluster operation use cases.

## Env Var Contract Summary

This table shows the full env var contract between the orchestrator (Temporal spawner) and the sandbox, aligned with the operator's generic env var convention [spec 16a] where applicable:

| Env var | Set by | Read by sandbox | Status |
|---|---|---|---|
| `LIGHTSPEED_PROVIDER` | Spawner | ✓ (`config.py:resolve_sdk()`) | Working |
| `LIGHTSPEED_MODEL` | Spawner | ✓ (`config.py` → SDK-specific var) | Working |
| `LIGHTSPEED_MODEL_PROVIDER` | Spawner (vertex only) | ✓ (`config.py` → vertex routing) | Working |
| `LIGHTSPEED_PROVIDER_URL` | Spawner (when set) | ✓ (`config.py`) | Working |
| `LIGHTSPEED_PROVIDER_PROJECT` | Spawner (vertex) | ✓ (`config.py`) | Working |
| `LIGHTSPEED_PROVIDER_REGION` | Spawner (vertex/bedrock) | ✓ (`config.py`) | Working |
| `LIGHTSPEED_PROVIDER_API_VERSION` | Spawner (azure) | ✓ (`config.py`) | Working |
| `LIGHTSPEED_SKILLS_DIR` | Default `/app/skills` | ✓ (`app.py`) | Working |
| `LIGHTSPEED_AUDIT_ENABLED` | Spawner (optional) | ✓ (`app.py`) | Working |
| `LIGHTSPEED_MCP_SERVERS` | Spawner | ✗ | **Adaptation needed** (MCP support) |
| Credentials | K8s Secret → `/var/run/secrets/llm-credentials/` | ✓ (via `config.py` + SDK) | Working |

## Spawner Changes

### KubernetesSpawner

```python
async def spawn(self, name, image, env, secret_env_vars, 
                skills_image=None, skills_paths=None, config=None):
    """Create K8s Job with sandbox image + skills OCI volume."""
    
    volumes = []
    volume_mounts = []
    
    # Skills OCI image volume
    if skills_image:
        volumes.append({
            "name": "skills",
            "image": {"reference": skills_image},
        })
        if skills_paths:
            for path in skills_paths:
                segment = path.rstrip("/").rsplit("/", 1)[-1]
                volume_mounts.append({
                    "name": "skills",
                    "mountPath": f"/app/skills/{segment}",
                    "subPath": path.lstrip("/"),
                })
        else:
            volume_mounts.append({
                "name": "skills",
                "mountPath": "/app/skills",
            })
    
    # ... create Job with env, secret_env_vars, volumes, mounts
```

### PodmanSpawner

```python
async def spawn(self, name, image, env, secret_env_vars,
                skills_image=None, skills_paths=None, config=None):
    """Create Podman container with sandbox image + skills volume."""
    
    mounts = []
    if skills_image:
        # Podman supports OCI image volumes natively
        mounts.append(f"type=image,src={skills_image},dst=/app/skills,ro=true")
    
    # ... create container with env, mounts
```

## Deployment

### OCP

```
FastAPI Deployment → Temporal Server (Helm) → Workers (Deployment)
                                                    ↓
                                              K8s Jobs (sandbox image)
                                              + Skills OCI image volumes
                                              + K8s Secrets for credentials
```

### Podman

```
FastAPI container → Temporal (podman-compose) → Workers (container)
                                                    ↓
                                              Podman containers (sandbox image)
                                              + OCI image volume mounts
                                              + Host env for credentials
```

For minimal Podman deployments: Temporal Lite (single binary, SQLite) replaces Temporal Server + PostgreSQL.

## What Product Teams Provide

| Artifact | Format | Example |
|---|---|---|
| Workflow definition | YAML | `diagnose-and-fix.yaml` |
| Agent instructions | Inline in YAML or separate AgentDefinition | System prompt text |
| Output schemas | JSON Schema in YAML | `{type: object, properties: {...}}` |
| Skills | OCI image with tool scripts | `quay.io/my-team/my-skills:latest` |
| MCP servers | HTTP endpoints | `https://servicenow.internal/mcp` |
| Credentials | K8s Secret or host env var | `OPENAI_API_KEY` in a Secret |

Product teams do NOT need to:
- Build container images (sandbox is pre-built)
- Write Python code (unless they want custom MCP servers)
- Understand Temporal internals (YAML is the interface)
- Fork any framework repo

## Comparison with Operator Approach

| Concern | Operator + Sandbox | Temporal + Sandbox |
|---|---|---|
| Workflow definition | Proposal CRD (fixed 4-step pipeline) | YAML (arbitrary steps, conditions, parallel) |
| Orchestrator | K8s controller (Go) | Temporal workflow (Python) |
| Agent runtime | Same sandbox | Same sandbox |
| Podman support | No | Yes |
| Human approval | ProposalApproval CRD | Temporal signal |
| State | etcd (CRDs) | Temporal Server (PostgreSQL) |
| Crash recovery | Controller-runtime reconcile loop | Temporal event replay |
| Agent config | Hardcoded Go templates | YAML (bring your own) |
| Scaling | Single controller replica | Horizontal Temporal workers |

The sandbox is the constant. The orchestrator is what changes. Both approaches call the same `POST /v1/agent/run` endpoint on the same container image.

## Goals & Requirements Coverage

Cross-reference with `GOALS.md`. Each goal and requirement is mapped to how (or whether) this architecture addresses it.

### Goals

| Goal | Coverage | How |
|---|---|---|
| **G1: Bring your own agents & workflows** | Covered | YAML workflow definitions + sandbox runtime. Product teams provide prompts, schemas, skills, MCP servers. No framework changes. |
| **G2: Secured & governed execution** | Covered | Ephemeral sandbox pods per step, per-step ServiceAccount RBAC (R13), approval gates via Temporal signals, risk_level classification, secrets via K8s SecretKeyRef. OTel tracing + SSE events (R14). |
| **G3: Composable agent ecosystem** | Covered | Agents-as-tools via registry, multiple triggers (API, chatbot, alerts, scheduled). See "G3 Design" below. |
| **G4: Seamless human-agent handoff** | Covered | Escalation context packaging with CLI bootstrap. See "G4 Design" below. |
| **G5: Dual deployment** | Covered | K8s spawner + Podman spawner. Temporal Server runs on both via Helm / podman-compose. Temporal Lite for minimal Podman. |

### Requirements

| Req | Coverage | How |
|---|---|---|
| **R1: Framework, not pre-built agents** | Covered | Sandbox is the generic runtime. Agent identity from mounted config. |
| **R2: Multi-step workflows with oversight** | Covered | Temporal workflow with conditions, retry, parallel groups, approval signals. |
| **R3: Ephemeral-by-default** | Covered | One sandbox pod per step, destroyed after execution. Pre-deployed option via `spawn: pre-deployed`. |
| **R4: Human-in-the-loop** | Covered | Temporal signals for approve/deny. Risk-based auto-approval policy. Structured output with confidence, risk, rollback. |
| **R5: Human-out-of-the-loop (CLI handoff)** | Designed below (G4) | Escalation packages context for interactive AI CLI session. |
| **R6: Retry with escalation** | Covered | Temporal RetryPolicy for infra errors. `build_escalation_activity` for exhausted retries. |
| **R7: Stateless runner, durable state** | Covered | Temporal workers are stateless. State in Temporal Server (PostgreSQL). |
| **R8: Execution engine (sandbox + Temporal)** | Covered | Core of this document. |
| **R9: Runtime (Pydantic AI + FastAPI + Temporal)** | Covered | FastAPI as Temporal client. Sandbox runs Pydantic AI (or Claude Code/OpenAI/Gemini). |
| **R10: Deployment (K8s + Podman)** | Covered | K8s Jobs + Services. Podman containers + network. One sandbox image. |
| **R11: Persistence with optimistic locking** | Covered | Temporal Server persistence. Single-threaded workflow execution eliminates CAS races. |
| **R12: Security** | Covered | Secrets via K8s refs, bearer auth, explicit risk_level, per-step permissions. |
| **R13: Access control** | Designed below | RBAC for who can trigger, approve, view workflows. |
| **R14: Observability** | Covered | OTel tracing, Temporal Web UI, query-based SSE for progress events. Prometheus metrics from sandbox. |
| **R15: Multiple triggers** | Designed below (G3) | API, chatbot, alerts, scheduled. |
| **R16: Agents-as-tools** | Designed below (G3) | Registry auto-generates tools from workflow definitions. |
| **R17: Escalation with CLI handoff** | Designed below (G4) | Context packaging + handoff to interactive session. |

### G3 Design: Composable Agent Ecosystem

G3 requires agents and workflows to be reusable building blocks with multiple trigger points.

**Multiple triggers (R15):**

| Trigger | Mechanism |
|---|---|
| **API** | `POST /workflows/run` — FastAPI starts a Temporal workflow. Already designed. |
| **Chatbot** | Chatbot tool calls `POST /workflows/run` with the user's query as `input_prompt`. The workflow runs asynchronously; the chatbot polls status via `GET /workflows/:id/events` and streams results back to the user. |
| **Alerts** | An alerts adapter (like `lightspeed-agentic-alerts-adapter`) watches Alertmanager webhooks and creates workflow runs for matching alerts. Same pattern as the operator's adapter — trigger is external, execution is via the standard API. |
| **Scheduled** | Temporal's native `schedule` API starts workflows on a cron schedule. No custom scheduler needed. |

**Agents-as-tools (R16):**

The agent/workflow registry auto-generates Pydantic AI tool definitions from registered workflows:

```python
# Auto-generated tool from a registered workflow
@tool_plain
async def run_diagnose_workflow(prompt: str) -> dict:
    """Run the diagnose-and-fix workflow and return results."""
    handle = await temporal_client.start_workflow(
        AgentWorkflow.run,
        args=[WorkflowInput(definition=definitions["diagnose-and-fix"], prompt=prompt)],
        id=f"tool-{uuid4()}",
        task_queue="agents",
    )
    return await handle.result()
```

A chatbot agent can call other workflows as tools, enabling composable multi-agent pipelines. The registry provides tool metadata (name, description, input schema) for each registered workflow definition.

### G4 Design: Human-Agent Handoff

G4 requires seamless handoff from automated workflows to interactive AI CLI sessions (Claude Code, Goose) when automation reaches its limit.

**Escalation handoff (R5, R17):**

When retries exhaust, the `build_escalation_activity` packages full workflow context:

```python
@activity.defn
async def build_escalation_activity(steps: dict[str, StepResult]) -> StepResult:
    """Package workflow context for human handoff."""
    handoff = {
        "type": "escalation_handoff",
        "diagnosis": steps.get("diagnosis", {}).output if "diagnosis" in steps else None,
        "actions_attempted": [
            {"step": k, "output": v.output, "error": v.error}
            for k, v in steps.items() if v.status == "failed"
        ],
        "failure_history": [
            {"step": k, "error": v.error}
            for k, v in steps.items() if v.status == "failed"
        ],
        "cluster_context": {
            "namespaces": steps.get("diagnosis", {}).output.get("targetNamespaces", [])
                if "diagnosis" in steps and steps["diagnosis"].output else [],
        },
        # CLI session bootstrap command
        "handoff_command": "claude --resume-from /tmp/escalation-context.json",
    }
    return StepResult(status="escalated", output=handoff)
```

**Interactive session bootstrap:**

The escalation output includes a `handoff_command` that launches an AI CLI session pre-loaded with the workflow's context. The FastAPI service exposes an endpoint to download the escalation context:

```
GET /workflows/:id/escalation → JSON context file
```

The user downloads and runs:
```bash
# Download escalation context and start interactive session
curl -s https://lightspeed.cluster/workflows/wf-123/escalation > /tmp/context.json
claude --resume-from /tmp/context.json
# or: goose session start --context /tmp/context.json
```

The CLI session has full visibility into what was tried, what failed, and what tools/namespaces are relevant — enabling the human to continue investigation where the automated workflow left off.

**Implementation note**: The exact CLI handoff mechanism depends on which AI CLI the team uses (Claude Code, Goose, etc.) and their respective context-loading APIs. The framework produces the context package; the handoff UX is team-specific.

### R13 Design: Access Control

Workflow access control operates at the FastAPI layer, before Temporal:

| Action | Control mechanism |
|---|---|
| **Trigger a workflow** | Bearer auth on `POST /workflows/run`. The token identifies the user/team. An RBAC policy maps tokens to allowed workflow definitions. |
| **Approve a step** | Bearer auth on `POST /workflows/:id/approve`. The approval policy specifies which roles can approve which risk levels. |
| **View workflow status** | Bearer auth on `GET /workflows/:id`. Namespace-scoped visibility — users see only workflows in their namespace/team. |
| **Tool invocation** | Per-step `permissions.allowed_tools` / `permissions.denied_tools` in workflow YAML. Enforced by the framework before dispatching to the sandbox. |

**K8s integration**: On OCP, bearer tokens can be K8s ServiceAccount tokens validated via TokenReview API. On Podman, tokens are shared secrets from env vars.

**Implementation sequence**: Phase 2 includes bearer auth on all endpoints (already partially done via `BearerAuthMiddleware`). Full RBAC policy (who can trigger which workflows, who can approve which risk levels) is a Phase 5 deliverable.

**Known limitation in Phases 2-4**: Authentication is enforced (bearer tokens required) but authorization is not — any authenticated user can trigger any workflow and approve any step. This is acceptable for initial development and single-team deployments. Multi-team production deployments require Phase 5 authorization before onboarding.

## Implementation Sequence

### Phase 1: Sandbox adaptations (1-2 days, coordinated with Lightspeed Agentic team)
- ~~Add `/healthz` alias~~ (not needed — spawner uses `/health` which sandbox already exposes)
- Add `LIGHTSPEED_MODEL` and `LIGHTSPEED_PROVIDER` env var reading
- Submit as PR to `openshift/lightspeed-agentic-sandbox`

### Phase 2: Temporal + sandbox integration (4-6 weeks)
- Implement `AgentWorkflow` Temporal class
- Implement `run_sandbox_step` activity with spawner integration
- Add OCI image volume support to `KubernetesSpawner` and `PodmanSpawner`
- Implement context building (`build_sandbox_context`)
- Integration test: Temporal workflow → sandbox pod → real LLM on Kind
- Integration test: same on Podman

### Phase 3: MCP server support in sandbox (1-2 weeks, coordinated)
- Implement `LIGHTSPEED_MCP_SERVERS` reading in the sandbox
- Add MCP toolset creation per provider
- Integration test: workflow step calling an MCP tool server

### Phase 4: Composability + triggers (G3) (2-3 weeks)
- Implement agents-as-tools: auto-generate Pydantic AI tools from workflow registry (R16)
- Implement Temporal schedule-based triggers for periodic workflows (R15)
- Implement alerts adapter integration (webhook → workflow run) (R15)
- Integration test: chatbot agent calling a workflow as a tool

### Phase 5: Access control + escalation handoff (G4) (2-3 weeks)
- Implement full RBAC policy on FastAPI endpoints (R13)
- Implement escalation context packaging with CLI handoff (R5, R17)
- Document CLI handoff for Claude Code and Goose
- Integration test: escalation → CLI session with pre-loaded context

### Phase 6: Product team onboarding
- Publish workflow YAML authoring guide
- Publish skills packaging guide (OCI image volumes)
- Onboard first product team (Ansible or RHDH) with a sample workflow
