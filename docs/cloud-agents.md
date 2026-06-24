# Feature design for Cloud Agents PoC

|                    |                                           |
|--------------------|-------------------------------------------|
| **Date**           | 2026-06-17                                |
| **Component**      | playground / agent framework              |
| **Authors**        | James Wong                                |
| **Feature**        | Cloud Agents — server-side multi-agent platform |
| **Spike**          | N/A (PoC-driven exploration)              |
| **Links**          | [PoC requirements](server-side-agents-poc.md), [Architecture](pydantic-ai-architecture.md), [Diagnose-and-fix feasibility](diagnose-and-fix-feasibility.md), [Strategic direction](strategic-direction.md), [Phase 1a tasks](phase-1a-tasks.md) |

## What

An **agent/workflow framework** built on Pydantic AI and pydantic-graph within lightspeed-stack. The framework enables product teams to **create, deploy, and manage** their own cloud agents and multi-step workflows — running as server-side services in customer clusters (OCP/K8s or Podman).

The diagnostic, monitoring, and deploy-readiness agents are **reference implementations** demonstrating the framework's capabilities. Product teams (Ansible, RH Developer Hub, OpenStack) use the framework to build domain-specific agents for their own use cases.

## Why

Lightspeed-stack today is a **single conversational agent** behind a chat UI. The emerging need (from OpenStack, AAP, and RHDH teams) is for lightspeed-stack to function as a **cloud agent platform** — multiple agents per deployment, each with different roles, running where the infrastructure is.

Key drivers:
- **OpenStack** needs an RCA agent and an upgrade-assistance agent (September 2026 timeline)
- **AAP** needs autonomous monitoring agents for deployment pipeline health
- **Strategic positioning**: if LCS only proxies queries to Llama Stack, local agents with OKP access make it redundant. Cloud agents are the differentiator.

Without this PoC, the team cannot validate whether Pydantic AI is the right framework for the multi-agent, long-running, autonomous agent patterns these use cases require.

## Requirements

- **R1:** Multiple agent types run in a single process — monitoring, diagnostic, and conversational agents coexist and can be invoked independently.
- **R2:** Autonomous monitoring agent runs in a loop, polls simulated cluster state, and detects anomalies without human prompting.
- **R3:** Agent collaboration — monitoring agent dispatches diagnostic agent with context when an anomaly is detected. The diagnostic agent receives the alert and investigates autonomously.
- **R4:** Diagnostic agent executes multi-step remediation (discover → diagnose → fix → verify) using the agentic tool loop, with a quality gate (`output_validator`) that rejects incomplete work.
- **R5:** Human-in-the-loop approval gate — remediation actions require approval before execution. Simulated in the PoC, but the mechanism (tool-based `ModelRetry` on rejection) must be demonstrated.
- **R6:** Conversational agent delegates investigation to the diagnostic agent when a user asks a question requiring active cluster inspection (not just RAG lookup).
- **R7:** Predictive detection — an agent identifies a trend in metrics (e.g., disk growth rate) and triggers preemptive action before the threshold is breached.
- **R8:** Structured output — all agents return Pydantic models (`MonitoringAlert`, `DiagnosticReport`), not free text. Application code can inspect fields programmatically.
- **R9:** Step-by-step visibility — each agent's tool calls and results are visible in real-time via `agent.iter()`, suitable for building progress UIs.
- **R10:** Simulated cluster with mutable state — hosts, services, metrics, and events that change over time (drift simulation). Agent actions (remediation) mutate the state, and verification reflects the mutation.

## Use Cases

- **U1:** As a cluster operator, I want an autonomous agent monitoring my cluster health, so that issues are detected and investigated without me having to manually check dashboards.
- **U2:** As a cluster operator, I want the monitoring agent to automatically dispatch a diagnostic agent when it detects an anomaly, so that root cause analysis begins immediately.
- **U3:** As a cluster operator, I want to ask the conversational agent "what's wrong with my cluster?" and get an active investigation (not just a RAG answer), so that I get actionable findings based on current cluster state.
- **U4:** As a cluster operator, I want remediation actions to require my approval before execution, so that the agent doesn't make destructive changes without oversight.
- **U5:** As a cluster operator, I want the system to predict issues (like disk filling up) and take preemptive action, so that I avoid outages caused by foreseeable problems.
- **U6:** As a platform developer consuming lightspeed-stack, I want structured reports from agents (not free text), so that I can build dashboards and alerts on top of agent findings.

## Architecture

### Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                  Simulated Cluster State                     │
│  hosts: web-01, web-02, db-01, cache-01                     │
│  services: nginx, app, postgresql                            │
│  metrics: cpu, memory, disk (with drift)                     │
│  events: deploys, alerts, threshold crossings                │
└───────────────┬──────────────────┬──────────────┬───────────┘
                │                  │              │
     ┌──────────▼────────┐  ┌─────▼──────┐  ┌───▼────────────┐
     │ Monitoring Agent   │  │ Diagnostic │  │ Conversational │
     │                    │  │ Agent      │  │ Agent          │
     │ Loop:              │  │            │  │                │
     │  poll metrics      │  │ Tools:     │  │ Tools:         │
     │  detect anomaly ───┼─►│  check_host│  │  investigate   │
     │  dispatch diag     │  │  get_alerts│  │  _cluster ────►│
     │                    │  │  get_deploy│  │ (delegates to  │
     │ Output:            │  │  remediate │  │  diag agent)   │
     │  MonitoringAlert   │  │  verify    │  │                │
     └────────────────────┘  │            │  │ Output:        │
                             │ Quality:   │  │  str (user-    │
                             │  output_   │  │  facing text)  │
                             │  validator │  └────────────────┘
                             │            │
                             │ Output:    │
                             │  Diagnostic│
                             │  Report    │
                             └────────────┘
```

**Data flow for Scenario 1 (autonomous detection):**

```text
monitoring_loop() ──► monitoring_agent.run("Check all hosts")
                           │
                           ▼
                      MonitoringAlert(host="web-02", severity="critical")
                           │
                           ▼
                      dispatch_diagnostic(alert)
                           │
                           ▼
                      diagnostic_agent.iter("Investigate web-02...")
                           │
                           ├── [tool] check_host(web-02)
                           ├── [tool] get_recent_deploys()
                           ├── [tool] run_remediation(rollback)
                           │         └── [approval] AUTO-APPROVED
                           ├── [tool] check_host(web-02) ← verify
                           │
                           ▼
                      DiagnosticReport(cluster_healthy=True, actions=[...])
```

**Data flow for Scenario 2 (user-triggered):**

```text
user: "Why are responses slow?"
       │
       ▼
  conversational_agent.run(question)
       │
       ├── [tool] investigate_cluster(question)
       │         │
       │         ▼
       │    diagnostic_agent.run(question)
       │         │
       │         ├── [tool] list_hosts()
       │         ├── [tool] check_host(web-01)
       │         ├── [tool] check_host(web-02)
       │         ├── [tool] check_host(db-01)
       │         │
       │         ▼
       │    DiagnosticReport → JSON string returned to conversational agent
       │
       ▼
  conversational_agent formats findings for user
```

**Data flow for Scenario 3 (predictive preemption):**

```text
predictive_check() ──► analyze metrics trends
                           │
                           ▼
                      "db-01 disk will hit 95% in 6 hours"
                           │
                           ▼
                      diagnostic_agent.run("Preemptive cleanup on db-01")
                           │
                           ├── [tool] check_host(db-01)
                           ├── [tool] run_remediation(cleanup_disk)
                           ├── [tool] check_host(db-01) ← verify
                           │
                           ▼
                      DiagnosticReport(actions=[cleanup], cluster_healthy=True)
```

### Simulated cluster state

The PoC uses a mutable Python dictionary representing a cluster with:
- **4 hosts**: web-01 (healthy), web-02 (will degrade), db-01 (disk filling), cache-01 (healthy)
- **Services per host**: nginx, app, postgresql
- **Metrics**: cpu, memory, disk percentages
- **Events**: deploy log, alert log
- **Drift simulation**: functions that gradually change metrics to simulate real-world state evolution (disk filling, CPU spiking after deploy)

State mutations:
- `simulate_deploy(host, app, version)` — deploys an app, may cause CPU spike / crash
- `simulate_disk_growth(host, rate)` — gradually increases disk usage
- Remediation tools (`rollback_deploy`, `cleanup_disk`, `restart_service`) modify state and are verified by re-reading it

### Agent definitions

**Monitoring agent:**
- `output_type=MonitoringAlert` — structured: host, metric, severity, context
- No tools — only reads cluster state via a single tool (`get_cluster_summary`)
- Runs in a loop via application code, not the agent's own tool loop
- Dispatches diagnostic agent when severity is `high` or `critical`

**Diagnostic agent:**
- `output_type=DiagnosticReport` — structured: issues found, actions taken, remaining issues, cluster health
- Tools: `list_hosts`, `check_host`, `get_alerts`, `get_recent_deploys`, `run_remediation`
- `output_validator` quality gate: rejects report if hosts are still unhealthy or no actions were taken
- `run_remediation` includes simulated approval gate with logging
- Reuses patterns from `playground/try_diagnose_and_fix.py`

**Conversational agent:**
- `output_type=str` — natural language response for user
- Single tool: `investigate_cluster` which delegates to the diagnostic agent
- Uses `ctx.usage` to track tokens across the delegation chain

### Structured output models

```python
class MonitoringAlert(BaseModel):
    host: str
    metric: str                           # "cpu", "disk", "service_crash", etc.
    value: str                            # "92%", "crashed", etc.
    severity: Literal["low", "medium", "high", "critical"]
    context: str                          # what the monitoring agent observed
    recommended_action: str               # what it thinks should happen

class RemediationAction(BaseModel):
    host: str
    action: str                           # "rollback_deploy:frontend", "cleanup_disk"
    result: str
    success: bool

class DiagnosticReport(BaseModel):
    summary: str
    issues_found: list[str]
    actions_taken: list[RemediationAction]
    remaining_issues: list[str] = Field(default_factory=list)
    cluster_healthy: bool
```

### Trigger mechanism

Three trigger patterns demonstrated:

1. **Polling loop** (Scenario 1): `asyncio` loop calls the monitoring agent at intervals. Between polls, drift functions evolve cluster state. When the monitoring agent returns a high/critical alert, it dispatches the diagnostic agent.

2. **User request** (Scenario 2): Direct invocation of the conversational agent with a question. The agent decides whether to delegate to the diagnostic agent.

3. **Predictive check** (Scenario 3): Application code analyzes metric trends (disk growth rate), detects a future threshold crossing, and dispatches the diagnostic agent with a preemptive cleanup request.

### Error handling

- Diagnostic agent `output_validator` raises `ModelRetry` if the report claims healthy but hosts are still unhealthy, or if no remediation actions were taken. Up to `retries=3` attempts.
- `run_remediation` tool returns `{"success": False, "error": "..."}` for unknown hosts or actions — the agent sees the failure and can try a different approach.
- Unknown tool actions return error dicts rather than raising exceptions, keeping the agent loop intact.

## Acceptance test surface

| Req | Observable behavior | Verified by |
|-----|---------------------|-------------|
| R1  | Three agents (monitoring, diagnostic, conversational) run in the same process and produce output | Script completes all three scenarios without error |
| R2  | Monitoring agent detects web-02 degradation after simulated deploy | `MonitoringAlert` output with severity=critical for web-02 |
| R3  | Diagnostic agent runs after monitoring agent detects anomaly — not manually triggered | Console output shows `[monitor] Dispatching diagnostic agent...` followed by `[diag]` tool calls |
| R4  | Diagnostic agent calls tools, remediates, and verifies — cluster state changes from unhealthy to healthy | Final cluster state shows all hosts healthy after remediation |
| R5  | Approval gate logs appear before remediation executes | Console shows `[approval] Agent requests: ... → AUTO-APPROVED` |
| R6  | Conversational agent calls `investigate_cluster` tool when asked about cluster issues | Console shows conversational agent's tool call to diagnostic agent |
| R7  | Predictive check identifies disk growth trend and triggers preemptive cleanup before threshold | db-01 disk reduced before reaching 95% critical threshold |
| R8  | All agents return Pydantic models — `MonitoringAlert` and `DiagnosticReport` fields are printed individually | Structured field output visible in console (not free-text parsing) |
| R9  | Tool calls and results are printed step-by-step during diagnostic agent runs | Console shows `[diag] step N [tool] ...` and `[result] ...` lines |
| R10 | Cluster state mutates: web-02 recovers after rollback, db-01 disk drops after cleanup | Before/after state comparison printed at end of each scenario |

## Implementation Suggestions

### Key files and insertion points

| File | What to do |
|------|------------|
| `playground/try_server_agents.py` | **Create** — main PoC script with all three scenarios |
| `playground/common.py` | **Reuse** — `make_model()` for LLM backend configuration |

### Insertion point detail

The script is standalone — no changes to `src/` code. It imports only from:
- `playground.common` — `make_model()`
- `pydantic_ai` — `Agent`, `RunContext`, `ModelRetry`, graph node types for `iter()`
- `pydantic` — `BaseModel`, `Field` for structured output models

### Code structure

```python
# playground/try_server_agents.py

# 1. Imports and constants

# 2. Structured output models
#    - MonitoringAlert
#    - RemediationAction  
#    - DiagnosticReport

# 3. Simulated cluster state
#    - cluster_state dict (hosts, services, metrics, events, deploys)
#    - reset_cluster() — initialize to known state
#    - simulate_deploy(host, app, version) — trigger a bad deploy
#    - simulate_disk_growth(host, amount) — gradually fill disk
#    - action_log list — audit trail of remediation actions

# 4. Diagnostic agent (reuse pattern from try_diagnose_and_fix.py)
#    - Agent with output_type=DiagnosticReport, retries=3
#    - Tools: list_hosts, check_host, get_alerts, get_recent_deploys, run_remediation
#    - output_validator: verify_all_fixed
#    - run_remediation includes approval gate simulation

# 5. Monitoring agent
#    - Agent with output_type=MonitoringAlert
#    - Single tool: get_cluster_summary (returns all host statuses)
#    - No remediation capability — detection only

# 6. Conversational agent
#    - Agent with output_type=str
#    - Tool: investigate_cluster (delegates to diagnostic agent via ctx.usage)

# 7. Helper: iter_diagnostic_with_visibility(prompt)
#    - Wraps diagnostic_agent.iter() with step-by-step console output
#    - Reuse pattern from try_agent_loop.py

# 8. Scenario 1: Autonomous detection and fix
#    - reset_cluster() to healthy state
#    - simulate_deploy("web-02", "frontend", "v2.3.1") — causes crash
#    - monitoring_agent.run() detects anomaly
#    - dispatch diagnostic agent with alert context
#    - print before/after cluster state

# 9. Scenario 2: User-triggered investigation
#    - reset_cluster() with pre-existing issues
#    - conversational_agent.run("Why are responses slow?")
#    - print conversational response (which includes diagnostic findings)

# 10. Scenario 3: Predictive preemption
#     - reset_cluster() to healthy state  
#     - simulate_disk_growth("db-01", amount) — disk at 82%, trending up
#     - Application code calculates: "at current rate, 95% in 6 hours"
#     - dispatch diagnostic agent with preemptive cleanup request
#     - print before/after cluster state

# 11. main() — runs all three scenarios sequentially
```

### Console output format

Each scenario follows this format:

```text
=== Scenario N: <Title> ===

Initial state:
  web-01: healthy (cpu=45%, disk=78%)
  web-02: healthy (cpu=35%, disk=45%)
  ...

[event] <what triggered the scenario>

[monitor] Checking cluster health...
[monitor] ALERT: <structured alert fields>
[monitor] Dispatching diagnostic agent...

  [diag] step 1 [tool] <tool_name>(<args>)
         [result] <truncated result>
  ...
    [approval] Agent requests: <action> on <host>
               Reason: <reason>
               → AUTO-APPROVED
  ...

REPORT:
  Summary: <report.summary>
  Issues: <report.issues_found>
  Actions: <report.actions_taken>
  Cluster healthy: <report.cluster_healthy>

Final state:
  web-01: healthy (cpu=45%, disk=78%)
  web-02: healthy (cpu=40%, disk=45%)  ← recovered
  ...
```

### Test patterns

This is a PoC, not production code. Verification is by running the script and observing output:

```bash
# Run all scenarios
uv run python playground/try_server_agents.py

# Run with OpenAI instead of Ollama
PLAYGROUND_PROVIDER=openai OPENAI_API_KEY=sk-... uv run python playground/try_server_agents.py
```

No unit tests are created for playground scripts. The acceptance test surface above defines what to observe in the output.

## Resolved Questions

*Questions from earlier drafts that are now answered in Phase 1 detail.*

- **~~Inter-agent communication protocol~~**: **HTTP with `/run` + `/healthz` endpoints.** Works in both OCP and Podman, no additional infrastructure, compatible with Pydantic AI patterns. — Resolved by evaluator recommendation, 2026-06-17.

- **~~Agent configuration schema (Phase 1)~~**: **Minimal YAML in `lightspeed-stack.yaml`** with name, endpoint, type, skills, resources, trigger. Full `AgentDefinition` CRD deferred to Phase 2. — Resolved in Phase 1 spec, 2026-06-17.

- **~~Agent lifecycle management~~**: **K8s-native on OCP** (Deployments, readiness probes, restart policies), **compose-native on Podman**. Core pod maintains in-memory agent registry. — Resolved in Phase 1 spec, 2026-06-17.

- **~~Skills integration with multi-agent~~**: **Per-agent skill sets.** Each agent pod gets its own `SkillsCapability` with only its prescribed skills mounted as a read-only volume. No shared registry in Phase 1. — Resolved in Phase 1 spec, 2026-06-17.

## Open Questions for Future Work

- **Durable execution**: Which persistence backend (pydantic-graph `FileStatePersistence`, DBOS, Temporal) fits lightspeed-stack's production deployment? — Origin: diagnose-and-fix feasibility assessment, updated 2026-06-17.

- **Real human approval flow**: How does an agent pause mid-workflow, persist state, wait for Slack/web approval, and resume? pydantic-graph node-level persistence supports this, but the UX flow is undesigned. — Origin: JR/James conversation 2026-06-17.

- **Sandboxing**: If agents generate scripts or commands, where do they execute safely? JR proposed server-side sandboxes that verify before real execution. Needs a spike. — Origin: JR/James conversation 2026-06-17.

- **Centralized quota management**: When N agent pods each connect to the LLM independently, how is token quota enforced globally? Phase 1 uses per-pod quota (acceptable for 2-3 agents). Phase 2 may need a centralized quota service. — Origin: evaluator assessment, 2026-06-17.

- **`/responses` API deprecation**: If LCS shifts to cloud agents, the `/responses` proxy becomes less relevant. Needs team alignment. — Origin: JR/James conversation 2026-06-17.

- **Predictive analytics**: The PoC simulates trend detection with application code. Real predictive agents need time-series analysis, possibly ML models. — Origin: PoC requirements doc.

- **OCP vs Podman config divergence**: OCP has CRDs, ServiceAccounts, NetworkPolicy, readiness probes; Podman has none. Supporting both may require a lowest-common-denominator approach or separate config surfaces. — Origin: evaluator assessment, 2026-06-17.

## Roadmap: From PoC to Production

This PoC (Phase 0) validates the core patterns. The following phases outline the path from playground script to production cloud agent platform. Each phase builds on the previous — the code and architecture decisions made earlier must support what comes later.

### Phase 0: PoC — Validate patterns (current)

**Goal:** Prove Pydantic AI can support multi-agent, autonomous, collaborative agents.

- Single Python process, simulated cluster
- Prescribed agents with hardcoded tools/skills
- Simulated approval gates
- No persistence, no deployment, no sandboxing
- **Deliverable:** `playground/try_server_agents.py`

### Phase 1: Sandboxed agent pods — Prescribed agents in production

**Goal:** Each agent runs in its own isolated container with prescribed skills/tools, communicating with the core pod over HTTP.

**Effort estimate:** Full quarter (10-12 weeks, 2-3 engineers).

**Non-goals for Phase 1:** User-defined agents (Phase 2), AI-generated workflows (Phase 3), CRDs, dynamic agent creation at runtime, real human approval flows (Slack/web UI).

#### Dependencies

These must land before Phase 1 implementation starts:

| Dependency | Ticket | Why |
|-----------|--------|-----|
| Endpoint swap to Pydantic AI | LCORE-2310, LCORE-2311 | Agents must use `Agent.run()`, not `client.responses.create()` |
| Skills wiring into request flow | LCORE-2076 | Agent pods need skills support via `SkillsCapability` |
| Bridge merged | LCORE-2309 | `build_agent()` is the foundation for agent construction — **done** |

#### Critical design decision: Inter-agent communication

**Decision: HTTP with a thin internal API.**

The Phase 0 PoC uses in-process function calls (`diag_agent.run()` inside `@conv_agent.tool`). This **breaks across pod boundaries** — Pydantic AI `Agent` objects are process-local. Phase 1 replaces direct calls with HTTP:

```
Phase 0 (in-process):                    Phase 1 (cross-pod):

conv_agent                                conv_agent (core pod)
  @tool                                     @tool
  async def investigate(ctx, q):            async def investigate(ctx, q):
    result = diag_agent.run(q)                resp = await http.post(
    return result.output                        "http://diag-agent:8080/run",
                                                json={"prompt": q, "context": {...}}
                                              )
                                              return resp.json()["output"]
```

Each agent pod exposes two HTTP endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/run` | POST | Accept a prompt + context, run the agent, return structured result |
| `/healthz` | GET | Readiness check — returns 200 when agent is initialized |

**Why HTTP over alternatives:**
- Works in both OCP (K8s Service) and Podman (shared network) with no additional infrastructure
- Compatible with Pydantic AI's patterns — the core pod's delegation tool becomes an HTTP client
- No message broker dependency (Redis, NATS) to deploy and maintain
- Simple to debug (curl, logs)
- Async patterns (monitoring fire-and-forget) can be added later via a message queue without changing the synchronous path

#### Agent isolation model

Each agent runs in its own container with:
- **Prescribed skills** — mounted as read-only volume or ConfigMap
- **Prescribed tools** — MCP servers, CLI tools, API access defined in config
- **No access** to tools/skills not assigned to it
- **Resource limits** — CPU, memory per container
- **Token budget** — `max_tokens_per_run` enforced by the agent process

```
lightspeed-stack deployment
├── core pod (API gateway, /query, /streaming_query, auth, RAG, conversation mgmt)
│   ├── agent registry (in-memory map: agent-name → endpoint URL)
│   ├── RemoteAgentTool (HTTP client for calling agent pods)
│   └── conversational agent (runs in-process in core pod)
│
├── agent pod: diagnostic-agent
│   ├── HTTP server (/run, /healthz)
│   ├── Pydantic AI Agent with tools: check_host, get_alerts, run_remediation
│   ├── skills: [openshift-troubleshooting] (volume mount)
│   ├── output_validator quality gate
│   └── service account: edit role (can modify resources)
│
└── agent pod: monitoring-agent
    ├── HTTP server (/run, /healthz)
    ├── Pydantic AI Agent with tools: get_cluster_summary
    ├── periodic trigger (internal asyncio loop)
    ├── dispatches to diagnostic-agent via HTTP when alert severity >= high
    └── service account: view role (read-only)
```

#### Agent lifecycle management (Phase 1: static deployment)

*Revised after 3rd-party review (2026-06-17). Phase 1 uses a static deployment model — agents are predeployed via manifests/compose, not dynamically created by the core pod. Dynamic lifecycle management (core creates/destroys agent Deployments) is Phase 2 scope.*

| Lifecycle event | OCP | Podman |
|----------------|-----|--------|
| **Startup** | Agent Deployments pre-created via `kubectl apply` or Helm | `podman-compose up` starts all containers |
| **Readiness** | K8s readiness probe on `/healthz` | Core pod polls `/healthz` on startup |
| **Discovery** | **Config-driven** — `endpoint` URLs in `lightspeed-stack.yaml` | Same — `endpoint` URLs in config |
| **Crash recovery** | K8s `restartPolicy: Always` | Compose restart policy |
| **Shutdown** | SIGTERM with grace period | Same |
| **Scaling** | Manual `kubectl scale` | Manual |

**Agent registry:** The core pod reads agent endpoints from `lightspeed-stack.yaml` at startup and builds an in-memory map of `{agent_name: endpoint_url}`. No K8s Service discovery, no Pod event watching. The `endpoint` field in config is **required** and authoritative — this is the single source of truth for Phase 1.

#### Phase 1 configuration schema

Minimal — enough for the core pod to know what agents exist and how to reach them. Not the full Phase 2 `AgentDefinition` CRD.

```yaml
# lightspeed-stack.yaml (Phase 1 additions)
agents:
  - name: diagnostic-agent
    endpoint: http://diagnostic-agent:8080  # required — single source of truth
    type: diagnostic
    skills: [openshift-troubleshooting, root-cause-analysis]
    resources:
      max_tokens_per_run: 50000
      timeout_seconds: 600

  - name: monitoring-agent
    endpoint: http://monitoring-agent:8080
    type: autonomous
    trigger:
      type: periodic
      interval_seconds: 300
    dispatch_to: diagnostic-agent           # who to call on high/critical alerts
```

```python
# src/models/config.py (Phase 1 addition)
class AgentEndpointConfig(ConfigurationBase):
    """Configuration for a single agent pod."""

    name: str
    endpoint: AnyHttpUrl
    type: Literal["conversational", "diagnostic", "autonomous"]
    skills: list[str] = Field(default_factory=list)
    resources: Optional[AgentResourceConfig] = None
    trigger: Optional[AgentTriggerConfig] = None
    dispatch_to: Optional[str] = None
```

#### LLM backend access

Each agent pod needs its own connection to the LLM (via Llama Stack or direct provider). Two options:

**Option A: Each agent pod connects to Llama Stack independently.**
- Simpler — each pod has its own `LlamaStackProvider`
- Risk: N pods = N connections, quota management is per-pod not centralized
- Fits Phase 1 if agent count is small (2-3)

**Option B: Agent pods route LLM calls through the core pod.**
- Core pod acts as LLM proxy, manages quota centrally
- More complex, adds latency
- Better for Phase 2 when agent count may grow

**Recommendation for Phase 1:** Option A — each agent connects to Llama Stack directly. Centralized quota can be added in Phase 2 when the agent count justifies it.

#### Observability

Minimum requirements for Phase 1:

| Concern | Implementation |
|---------|---------------|
| **Distributed tracing** | OpenTelemetry trace context propagated in HTTP headers from core pod to agent pods. Each agent creates child spans. |
| **Correlation IDs** | A `correlation_id` header chains monitoring-alert → diagnostic-run → remediation-action across pods |
| **Structured logging** | Each agent logs with `agent_name`, `correlation_id`, and `run_id` fields |
| **Per-agent metrics** | Prometheus metrics per agent: tool calls, LLM tokens consumed, latency, errors |
| **Existing LCS metrics** | Extended with `agent_name` label dimension |

#### Security

| Concern | Phase 1 approach |
|---------|-----------------|
| **Pod RBAC** | OCP: dedicated ServiceAccount per agent pod. Monitoring = `view` role, diagnostic = `edit` role |
| **Secret injection** | Agent credentials (LLM API keys, cluster access tokens) via K8s Secrets mounted as env vars |
| **Network isolation** | OCP NetworkPolicy: agent pods can reach Llama Stack and cluster APIs, not each other (except monitoring → diagnostic) |
| **Tool restriction** | Each agent only has tools registered in its `Agent()` definition — enforced at code level, not container level |
| **Filesystem** | Agent containers run with read-only root filesystem; `/tmp` writable for transient state |

#### Migration path for existing consumers

**Existing `/query` and `/streaming_query` consumers see zero change.** The conversational agent continues to run in-process in the core pod (same as today, but using Pydantic AI via LCORE-2310/2311). Agent pods are purely additive — they add diagnostic and monitoring capabilities behind the scenes.

If the conversational agent needs to delegate to the diagnostic agent, it does so internally via `RemoteAgentTool`. The external API contract is unchanged.

#### Phase 1a task breakdown (TDD, reviewed)

*Reviewed by independent evaluator 2026-06-17. Key changes: removed premature `/query` wiring (blocked on LCORE-2310), baked diagnostic tools into image (generic loader deferred to Phase 2), renamed RemoteAgentTool → RemoteAgentClient, versioned endpoints (`/v1/run`). Full task details in plan file.*

**Container strategy:** Diagnostic agent image bakes in tools and skills. Generic runtime image with dynamic tool loading is Phase 2.

**LLM mocking:** Unit tests use Pydantic AI `FunctionModel` — no real LLM needed.

| Task | What | Tests | Files |
|------|------|-------|-------|
| 1. Shared models | `AgentRunRequest`, `AgentRunResponse`, `DiagnosticReport` | Serialization round-trips, validation | `src/agents/models.py` |
| 2. Agent runtime server | FastAPI app with `/v1/run` + `/healthz` | TestClient: healthz, run success, 422, 500 | `src/agents/runtime/server.py` |
| 3. Diagnostic agent | Tools + agent definition + simulated cluster state | Tool unit tests, `FunctionModel` integration test | `src/agents/diagnostic/` |
| 4. RemoteAgentClient | HTTP client for calling agent pods | Mocked httpx: success, timeout, 500, connection refused | `src/agents/remote_agent_client.py` |
| 5. Registry + config | Agent discovery + YAML config | Registry lookup, config validation | `src/agents/registry.py`, `src/models/config.py` |
| 6. Container image | Diagnostic agent Containerfile | Manual: podman build + curl | `deploy/diagnostic-agent/Containerfile` |
| 7. Kind/Podman deploy | Manifests + setup script | Manual: setup.sh + kubectl | `deploy/kind/`, `deploy/podman/` |
| 8. E2E tests | Cross-pod Gherkin scenarios | `make e2e-cloud-agents` (manual, not CI) | `tests/e2e/features/cloud_agents.feature` |

**Implementation order:** Tasks 1→2→3→4→5 (local code + unit tests) → 6→7 (containers) → 8 (E2E).

**Not in Phase 1a scope (deferred):**
- Wiring into `/query` endpoint (blocked on LCORE-2310)
- Generic agent runtime with dynamic tool loading (Phase 2)
- CI pipeline for E2E tests (Phase 1b)

**Phase 1a acceptance criteria:**
- Diagnostic agent runs in a separate container and accepts prompts via `POST /v1/run`
- `RemoteAgentClient` in the core pod can call it and deserialize the response
- Agent endpoint is configured in YAML, not hardcoded
- Kind cluster or Podman compose runs both pods successfully
- E2E tests pass against deployed containers

**Phase 1b (7 tasks, ~7.5 days): Monitoring agent + async + observability**

*Full task breakdown: [phase-1b-tasks.md](phase-1b-tasks.md)*

*Architectural decisions resolved by evaluator review (2026-06-18):*
- **Shared state**: Context-passing via HTTP dispatch, not shared service. Both pods pre-seed same scenario via env var.
- **Async /v1/run**: `Prefer: respond-async` header → 202 + run_id + polling. Backward-compatible with Phase 1a sync mode.
- **Observability**: Structured logging with correlation IDs + Prometheus counters. Full OpenTelemetry deferred to Phase 2.
- **Security hardening (RBAC, NetworkPolicy)**: Deferred to Phase 2 — scope creep for a PoC-validation phase.

| Task | What | Tests |
|------|------|-------|
| 1. Shared models + scenario init | `MonitoringAlert`, `MonitoringResult`, `RunState`, scenario-based state | ~6 |
| 2. Async /v1/run + polling | `Prefer` header, `/v1/runs/{run_id}`, `RunStore`, async client methods | ~10 |
| 3. Monitoring agent | Tools, agent definition, `MonitoringLoop` with periodic dispatch | ~12 |
| 4. Container + deploy | Monitoring agent Containerfile, updated Kind/Podman manifests | manual |
| 5. Liveness + timeout | `/livez` endpoint, `asyncio.wait_for()` around runs | ~4 |
| 6. Logging + metrics | Correlation IDs, Prometheus counters/histograms, `/metrics` | ~6 |
| 7. E2E tests | 4 new scenarios (monitoring health, detect, dispatch, full flow) | 4 E2E |

**Phase 1b acceptance criteria:**
- Monitoring agent runs autonomously in its own pod, detects anomalies, dispatches diagnostic agent
- Both pods pre-seed same cluster scenario; monitoring passes alert context to diagnostic via HTTP
- Async `/v1/run` with polling — sync mode backward-compatible
- `/livez` detects hung agents; runs exceeding timeout return 500
- Correlation IDs in all logs and responses; Prometheus metrics on `/metrics`
- 8 E2E scenarios pass across containers (4 from 1a + 4 new)
- Podman compose and Kind manifests deploy all pods

### Phase 2: User-defined agents — Configuration-driven agent creation

**Goal:** Product teams and advanced users can define and deploy new agents without code changes.

#### Who creates agents

| Role | What they can do |
|------|------------------|
| Platform developer (Red Hat) | Define built-in agents shipped with the product |
| Product team (Ansible, OpenShift) | Define domain-specific agents via config |
| Cluster admin (customer) | Customize agent parameters, enable/disable agents |
| End user | Interact with agents via chat UI — **cannot** create agents |

#### Agent definition format

Agents are defined in YAML (extending `lightspeed-stack.yaml` or as separate files):

```yaml
# agents/rca-investigator.yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentDefinition
metadata:
  name: rca-investigator
spec:
  type: diagnostic           # conversational | autonomous | diagnostic
  description: "Investigates and remediates cluster issues"
  model: granite-3.3          # override default model, or omit to use default
  instructions: |
    You are a root cause analysis agent. When triggered with an alert,
    investigate the affected hosts, identify the root cause, and attempt
    remediation with user approval.
  skills:
    - openshift-troubleshooting
    - root-cause-analysis
  tools:
    - name: oc-read
      type: mcp
      server: openshift-mcp-readonly
    - name: oc-write
      type: mcp
      server: openshift-mcp-readwrite
      requires_approval: true
  trigger:
    type: on-demand            # on-demand | periodic | event-driven
  resources:
    max_tokens_per_run: 50000
    max_tool_calls: 30
    timeout_seconds: 600
  sandbox:
    filesystem: read-only
    network_policy: cluster-internal-only
    allowed_commands: ["oc", "kubectl"]
```

#### Workflow definition (for multi-step prescribed workflows)

Users can define deterministic workflows that chain agents or skills:

```yaml
# workflows/cluster-rca.yaml
apiVersion: lightspeed.redhat.com/v1alpha1
kind: AgentWorkflow
metadata:
  name: cluster-rca
spec:
  description: "4-step RCA workflow: diagnose → recommend → approve → execute"
  steps:
    - name: diagnose
      agent: rca-investigator
      output_type: DiagnosticFindings
      skills: [cluster-diagnostics]

    - name: recommend
      agent: rca-investigator
      input_from: diagnose
      output_type: RemediationPlan
      skills: [remediation-planning]

    - name: approve
      type: human-approval
      input_from: recommend
      approval_channel: slack          # or: web-ui, cli
      timeout: 30m

    - name: execute
      agent: rca-investigator
      input_from: recommend
      condition: approve.result == "approved"
      skills: [remediation-execution]
      tools:
        - name: oc-write
          requires_approval: false     # already approved in step 3
```

This maps directly to the OpenShift Nexus CRD pattern (prescribed graph nodes) but expressed as lightspeed-stack config rather than K8s CRDs.

#### Key questions for Phase 2

- How are agent definitions validated before deployment? (Schema validation, dry-run, sandbox test)
- How do agents discover each other? (Registry, DNS, shared config)
- How are skills distributed to agent pods? (Git sync, ConfigMap mount, artifact registry)
- Version control for agent definitions? (GitOps, config versioning)
- How do users test their agent definition before deploying to production? (Dev mode, staging)

### Phase 3: AI-generated workflows — Agents that create agents

**Goal:** A meta-agent (or the conversational agent) can analyze a situation, design a multi-step workflow, and propose it for human approval before deploying.

This is the "agents building workflows" vision from JR's in-progress work.

#### How it works

```
User: "I need a workflow that monitors our etcd cluster,
       detects leader election stalls, and auto-restarts the
       affected node after approval."

       │
       ▼
  Workflow Designer Agent
       │
       ├── Analyzes: what skills/tools exist
       ├── Designs: multi-step workflow definition (YAML)
       ├── Validates: checks skill availability, tool permissions
       │
       ▼
  Proposed Workflow (structured output):

    steps:
      1. monitor etcd metrics (periodic, every 2 min)
      2. detect leader election stall (threshold: >30s)
      3. diagnose affected node (diagnostic agent with etcd-troubleshooting skill)
      4. propose restart (human approval via Slack)
      5. execute restart (oc delete pod, with rollback skill)
      6. verify recovery (check etcd cluster health)

       │
       ▼
  Human Review
       │
       ├── Approve → workflow deployed as Phase 2 AgentWorkflow
       ├── Modify → user edits YAML, resubmits
       └── Reject → workflow discarded
```

#### What the Workflow Designer Agent needs

- **Skill catalog awareness** — `list_skills` tool to know what's available
- **Tool registry awareness** — what MCP servers and tools are deployed
- **Agent template library** — existing agent definitions as examples
- **Workflow schema knowledge** — the Phase 2 `AgentWorkflow` spec as structured output
- **Validation tool** — dry-run the proposed workflow against current cluster config
- **Pydantic AI capability**: JR is building this as a Pydantic AI capability that gives agents awareness of other configured agents and workflows

#### Safety controls

| Control | Purpose |
|---------|---------|
| Human approval required before deployment | No autonomous workflow creation |
| Workflow must use only existing skills/tools | Can't invent new tools |
| Sandbox test run before production deployment | Validate on simulated state first |
| Audit log of who created/approved each workflow | Accountability |
| Workflow version control | Rollback if a workflow causes problems |
| Resource limits inherited from agent definitions | Can't create agents with unlimited access |

#### Key questions for Phase 3

- Can the LLM reliably generate valid workflow YAML? (Structured output + validation + retry helps, but complex schemas are hard)
- How does the designer agent know what tools/skills are safe to combine? (Permission model, conflict detection)
- Should generated workflows be stored as code (GitOps) or as runtime config? (Impacts auditability and rollback)
- How does this relate to JR's dynamic workflow planning capability? (His work may provide the Pydantic AI mechanics)

### Phase summary

| Phase | What | Agent creation | Workflow | Deployment | Sandboxing |
|-------|------|---------------|----------|------------|------------|
| **0 (done)** | PoC | Hardcoded in Python | Implicit (LLM decides) | Single process | None |
| **1a** | Diagnostic agent pod + HTTP comms | YAML config (minimal) | Implicit + skills-driven | Core pod + 1 agent pod | Container isolation |
| **1b** | Monitoring agent + lifecycle + hardening | YAML config (minimal) | Implicit + periodic triggers | Core pod + N agent pods | Container + RBAC + network policy + observability |
| **2** | User-defined | YAML config by product teams + admins | Explicit YAML workflows | Dynamic pod creation | Container + RBAC + network policy |
| **3** | AI-generated | LLM designs, human approves | LLM generates YAML, human approves | Auto-deployment after approval | Full sandbox + dry-run + audit |

### What the PoC code must anticipate

The Phase 0 PoC doesn't implement any of this, but its code structure should not preclude it:

1. **Agent definitions should be data, not hardcoded** — even if the PoC hardcodes agents, use a pattern where agent config (instructions, skills, tools) is separated from agent construction. This makes Phase 1's YAML-driven creation a natural evolution.

2. **Tool registration should be per-agent, not global** — each agent gets only its prescribed tools. Don't register all tools on all agents.

3. **The monitoring → diagnostic dispatch should be generic** — the handoff pattern should work for any agent-to-agent dispatch, not just the specific agents in the PoC.

4. **Structured output models should be reusable** — `MonitoringAlert` and `DiagnosticReport` should be defined in a way that Phase 1 can import them from a shared module.

5. **Approval gates should be pluggable** — the simulated approval in the PoC should use a pattern that can be replaced with a real approval flow (Slack, web UI) in Phase 1.

---

## Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-06-17 | Initial version | PoC spec based on prior exploration (architecture doc, diagnose-and-fix feasibility, JR/James strategic conversation) |
| 2026-06-17 | Added roadmap (Phases 0-3) | Capture forward-looking architecture: sandboxed pods, user-defined agents, AI-generated workflows |
| 2026-06-17 | Phase 1 rewritten with evaluator findings | Split into 1a/1b, resolved inter-agent comm (HTTP), added lifecycle, config schema, security, observability, migration path, effort estimate, dependencies |
| 2026-06-17 | Phase 1a task breakdown reviewed and revised | Independent evaluator review: removed premature /query wiring, baked tools into image, fixed package structure, renamed RemoteAgentClient, added FunctionModel mocking strategy |
| 2026-06-17 | 3rd-party review incorporated | Fixed: lifecycle is static deployment (not dynamic orchestrator), discovery is config-driven (no auto-discovery), simulated state is 1a-only (1b needs shared state), AgentRunResponse envelope strengthened with output_type + schema_version |

## Appendix A: Prior PoC Evidence

The following playground scripts validated individual patterns that this PoC combines:

| Script | Pattern validated | Result |
|--------|------------------|--------|
| `playground/try_pydantic_ai.py` | Basic chat, multi-turn, tools, structured output, streaming via LlamaStackProvider | All working |
| `playground/try_diagnose_and_fix.py` | Multi-step diagnostic workflow with tools, remediation, verification, `output_validator` quality gate, approval simulation | Agent autonomously fixed 2 hosts in 12 tool calls |
| `playground/try_multi_agent.py` | Agent delegation via `@agent.tool`, programmatic hand-off with structured data, unified token tracking | Router correctly delegates to specialists |
| `playground/try_agent_loop.py` | Agentic loop with 16+ autonomous tool calls, `agent.iter()` step-by-step visibility | Full tool visibility via node iteration |
| `playground/try_structured.py` | Complex nested Pydantic models, union output types for application branching | `Union[Solution, NeedMoreInfo]` branching works |
| `playground/try_skills.py` | Agent Skills progressive disclosure via `pydantic-ai-skills` `SkillsCapability` | Skills loaded on demand, not upfront |
| `playground/try_mcp.py` | In-process MCP servers via `MCPToolset`, multi-server with FastMCP | Todo + calculator MCP servers work together |

## Appendix B: Strategic Context

From the JR/James conversation (2026-06-17), the strategic positioning for lightspeed-stack:

- **LCS = cloud agents** — server-side, long-running, configurable agents in customer clusters
- **Not a local agent harness** — that's Goose, Claude Code, OpenCode
- **Not a proxy** — the `/responses` API makes LCS a passthrough; `/query` with cloud agents is the differentiator
- **Multi-agent per deployment** — one LCS instance, many agents with different roles
- **Customer-configurable** — product teams (Ansible, OpenShift, OpenStack) define agents via config; end users interact through chat UI or receive alerts

This PoC validates the technical feasibility of that vision using Pydantic AI.
