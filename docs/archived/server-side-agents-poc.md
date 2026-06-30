# Server-Side Agents for Lightspeed-Stack: PoC Design

**Date**: 2026-06-17
**Source**: James Wong / JR Boos conversation + prior PoC work
**Related**: `lcore-pydantic-ai-diagnose-and-fix.md`, `lcore-pydantic-ai-strategic-direction.md`

---

## What Are Server-Side Agents?

Lightspeed-stack today is a single conversational agent behind a chat UI. The vision is to evolve it into a **multi-agent platform** where agents run as long-lived processes inside the customer's cluster (in pods), with access to cluster internals that local agents (Goose, Claude Code) can never reach.

Server-side agents are different from local agents in three fundamental ways:

1. **They run where the infrastructure is** — inside the cluster, with direct access to APIs, metrics, logs, and services
2. **They run continuously** — not just when a user asks a question, but as persistent background processes
3. **They collaborate** — one agent detects a problem, another investigates it, another proposes a fix

---

## Agent Types

### 1. Conversational Agent (exists today)

The current Lightspeed experience. User asks a question via chat UI, agent responds using RAG + LLM.

```
User (chat UI) → Lightspeed /query → RAG + LLM → Response
```

**What changes:** The conversational agent gains access to other server-side agents. When a user asks "what's wrong with my cluster?", instead of just doing RAG lookup, it can dispatch to a diagnostic agent that actively investigates.

### 2. Autonomous Monitoring Agent

Runs continuously. Watches metrics, logs, or events. When it detects an anomaly, it can:
- Alert the user (via Slack, email, or the chat UI)
- Trigger another agent (e.g., kick off a diagnostic investigation)
- Take pre-approved actions (e.g., restart a crashed service)

**Concrete example from James:**
> "We have a testing suite running against the cloud version and staging. If there's any update in the staging image we execute test suites and sometimes it gets failures and then we have to diagnose why it failed. What would happen is if lightspeed-stack is running inside that cluster and we have those autonomous agents — those things would need this stack. You have some long-living server-side agent looking out for things, trying to work stuff out for you while your laptop might be moving around somewhere."

```
Monitoring Agent (loop)
  ├── Watch: CI test results, pod health, metrics
  ├── Detect: test failure, CPU spike, disk threshold
  ├── Action: alert user, trigger diagnostic agent, or auto-remediate
  └── Runs: continuously in the cluster pod
```

**Trigger patterns:**
- Periodic polling (every N minutes, check metrics)
- Event-driven (webhook from CI pipeline, Kubernetes event watch)
- Threshold-based (CPU > 90%, disk > 85%, test failure rate > 10%)

### 3. Diagnostic / RCA Agent

Triggered on-demand (by user or by monitoring agent). Investigates a specific issue using available tools. The diagnose-and-fix PoC already demonstrated this pattern.

```
Trigger (user or monitoring agent)
  → Diagnostic Agent
    → DISCOVER: scan hosts, services, logs
    → DIAGNOSE: correlate findings, identify root cause
    → RECOMMEND: propose remediation steps
    → [APPROVE]: human approval gate (optional)
    → [EXECUTE]: run remediation
    → VERIFY: check if fix worked
    → REPORT: structured summary of actions taken
```

**Concrete example from James (CI failure diagnosis):**
> "We have a CD deploy and the whole build pipeline — promote from staging, testing, then production. There are scenarios that I need to diagnose some of the issues. What I did is I have the OC command already locked in and then I use Claude Code to help me and Claude can just use OC command and drive through things."

The server-side version of this: the monitoring agent detects a CI failure, triggers the diagnostic agent, which uses `oc` commands (via MCP or direct tools) to investigate the cluster, produces a diagnosis, and either alerts the user or takes pre-approved action.

### 4. Heuristic / Predictive Agent

Runs periodically. Analyzes patterns over time. Predicts issues before they happen.

**Concrete example from James:**
> "You can have another agent to do heuristic — once in a while looking at certain sets of metrics. For example, traditionally if we have some database connection issues, we can have an agent focusing on that monitoring and try to predict whether they're going to happen with the current workload or not."

```
Predictive Agent (periodic)
  ├── Collect: time-series metrics, connection pool stats, query patterns
  ├── Analyze: trend detection, anomaly scoring, pattern matching
  ├── Predict: "connection pool exhaustion likely within 2 hours"
  ├── Recommend: "scale connection pool from 50 to 100" or "investigate slow query on table X"
  └── Runs: every 15-30 minutes
```

### 5. Housekeeping Agent

Background agent that performs routine maintenance tasks.

**Derived from James's description:**
> "You can have a housekeeping agent finding something — 'this is something anomaly or situation' — can you take this down and do that. And then you can have another agent to do heuristic."

```
Housekeeping Agent (periodic)
  ├── Check: disk usage, log rotation, stale pods, expired certificates
  ├── Clean: archive old logs, delete temp files, rotate secrets
  ├── Report: summary of actions taken
  └── Runs: daily or on-demand
```

---

## Agent Collaboration Patterns

### Pattern A: Monitoring → Diagnostic handoff

The most common pattern. Monitoring agent detects an issue, hands off to a diagnostic agent with the context it gathered.

```
Monitoring Agent
  │ detects: "web-02 CPU at 92%, app crashed after deploy v2.3.1"
  │
  ▼
Diagnostic Agent
  │ receives: alert context + host info
  │ investigates: check_host, get_logs, get_recent_deploys
  │ diagnoses: "deploy v2.3.1 introduced memory leak"
  │ recommends: "rollback to v2.3.0"
  │
  ▼
Human Approval (or auto-approve if pre-authorized)
  │
  ▼
Diagnostic Agent
  │ executes: rollback_deploy
  │ verifies: check_host → status: healthy
  │ reports: structured DiagnosticReport
```

### Pattern B: Conversational → Diagnostic delegation

User asks a question that requires active investigation. The conversational agent delegates to a diagnostic agent.

```
User: "My pods are slow, what's going on?"
  │
  ▼
Conversational Agent
  │ recognizes: this needs investigation, not just RAG lookup
  │
  ▼ delegates
Diagnostic Agent
  │ investigates cluster
  │ returns: structured findings
  │
  ▼
Conversational Agent
  │ formats findings for the user
  │ presents: "I found two issues: web-02 is overloaded after a recent deploy, 
  │            and db-01 is running out of disk. Want me to fix them?"
```

### Pattern C: Predictive → Housekeeping preemptive action

Predictive agent spots a trend, triggers housekeeping or diagnostic agent before the problem manifests.

```
Predictive Agent
  │ detects: "disk growth rate on db-01 → will hit 95% in 48 hours"
  │
  ▼
Housekeeping Agent
  │ action: archive old logs, purge temp tables
  │ result: disk freed from 82% to 60%
  │ report: "preemptive cleanup on db-01, next check in 24h"
```

### Pattern D: Agent creates skills for future use

From the conversation — an agent discovers a new pattern and creates a reusable skill.

> James: "I do stuff and I say 'hey, make a skill out of this' — or 'make a loop out of it, every day you do this for me.'"

```
Diagnostic Agent
  │ solves: novel issue (e.g., "etcd leader election stall during upgrade")
  │ recognizes: this is a reusable pattern
  │
  ▼
Skill Creator
  │ generates: SKILL.md with diagnostic steps
  │ stores: in skills repository (pending human review)
  │
  ▼
Human Review
  │ approves: skill is published
  │
  ▼
Future agents can now use this skill
```

---

## PoC Architecture

### What to build

A standalone PoC demonstrating server-side agents with Pydantic AI, exercising:
1. Multiple agents in a single process (not one-per-deployment)
2. Agent collaboration (monitoring triggers diagnostic)
3. Autonomous loop (monitoring agent runs continuously)
4. Human-in-the-loop approval gate
5. Structured reporting
6. Step-by-step visibility via `agent.iter()`

### Components

```
playground/try_server_agents.py

┌──────────────────────────────────────────────────────┐
│  Simulated Cluster State (mutable)                   │
│  - hosts, services, metrics, events                  │
│  - state changes over time (simulated drift)         │
└────────────────────┬─────────────────────────────────┘
                     │
       ┌─────────────┼──────────────┐
       │             │              │
       ▼             ▼              ▼
  ┌─────────┐  ┌──────────┐  ┌──────────────┐
  │Monitoring│  │Diagnostic│  │Conversational│
  │  Agent   │  │  Agent   │  │    Agent     │
  │          │  │          │  │              │
  │ - polls  │  │ - tools: │  │ - delegates  │
  │   metrics│  │   check  │  │   to diag    │
  │ - detects│  │   host,  │  │   agent      │
  │   anomaly│  │   logs,  │  │ - formats    │
  │ - triggers│ │   fix    │  │   response   │
  │   diag   │  │ - verify │  │   for user   │
  └────┬─────┘  └──────────┘  └──────────────┘
       │             ▲
       └─────────────┘
        triggers with context
```

### Simulated cluster

Reuse and extend the cluster state from `try_diagnose_and_fix.py`:
- Hosts with CPU, memory, disk, services
- Metrics that **drift over time** (disk slowly fills, CPU spikes on deploy)
- Event log that grows
- Deployments that can go wrong

### Monitoring agent

```python
monitoring_agent = Agent(
    make_model(),
    instructions="You are a cluster monitoring agent. Check host metrics periodically. "
                 "If any host is unhealthy or degraded, report the issue with context.",
    output_type=MonitoringAlert,  # structured: host, metric, severity, context
)
```

Runs in a loop (simulated with `asyncio.sleep`):
```python
async def monitoring_loop():
    while True:
        alert = await monitoring_agent.run("Check all hosts for issues.")
        if alert.output.severity in ("high", "critical"):
            await dispatch_diagnostic(alert.output)
        await asyncio.sleep(interval)
```

### Diagnostic agent

Reuse the diagnostic agent from `try_diagnose_and_fix.py` — tools for `check_host`, `get_alerts`, `run_remediation`, with `output_validator` quality gate.

### Conversational agent

Router that can answer simple questions directly (via RAG/skills) or delegate to the diagnostic agent for investigation:

```python
@conversational_agent.tool
async def investigate_cluster(ctx: RunContext, question: str) -> str:
    """Delegate to the diagnostic agent for active cluster investigation."""
    result = await diagnostic_agent.run(question, usage=ctx.usage)
    return result.output.model_dump_json()
```

### Demo scenarios

**Scenario 1: Autonomous detection and fix**
1. Start monitoring loop
2. Simulate: deploy v2.3.1 on web-02 → CPU spikes, app crashes
3. Monitoring agent detects degraded host
4. Dispatches diagnostic agent with context
5. Diagnostic agent: investigates → rolls back deploy → verifies → reports
6. Output: structured report of what happened and what was done

**Scenario 2: User-triggered investigation**
1. User asks conversational agent: "Why are responses slow?"
2. Conversational agent delegates to diagnostic agent
3. Diagnostic agent investigates all hosts
4. Returns findings to conversational agent
5. Conversational agent formats response for user

**Scenario 3: Predictive preemption**
1. Simulate: db-01 disk growing 2% per hour
2. Predictive check: "disk will hit 95% in 6 hours"
3. Triggers housekeeping: cleanup old logs
4. Verifies: disk back to safe level
5. Reports: preemptive action taken

### Output format

Each scenario should print step-by-step visibility:

```
=== Scenario 1: Autonomous Detection and Fix ===

[monitor] Checking cluster health...
[monitor] ALERT: web-02 degraded (cpu=92%, app=crashed)
[monitor] Dispatching diagnostic agent...

  [diag] step 1 [tool] check_host(web-02)
  [diag]         [result] cpu=92%, memory=88%, app=crashed
  [diag] step 2 [tool] get_recent_deploys(web-02)
  [diag]         [result] v2.3.1 deployed 5 minutes ago
  [diag] step 3 [tool] run_remediation(rollback_deploy:frontend, web-02)
  [diag]         [approval] AUTO-APPROVED
  [diag]         [result] Rolled back to v2.3.0
  [diag] step 4 [tool] check_host(web-02)
  [diag]         [result] cpu=40%, status=healthy

[monitor] Diagnostic complete. Cluster healthy.

REPORT:
  Issue: web-02 degraded after deploy v2.3.1
  Action: Rolled back frontend to previous version
  Result: Host recovered (cpu 92% → 40%)
  Cluster: healthy
```

---

## Technical Decisions for PoC

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent framework | Pydantic AI | Already integrated into lightspeed-stack; PoC builds on existing playground work |
| LLM backend | Ollama (qwen3.6) via LlamaStackProvider | Runs locally, free, already proven in prior PoCs |
| Multi-agent pattern | Delegation via `@agent.tool` | Proven in `try_multi_agent.py`; monitoring agent calls diagnostic agent |
| Monitoring loop | `asyncio` loop with simulated time | Real polling would need K8s events; simulate for PoC |
| Cluster state | Mutable Python dict with drift simulation | Same pattern as `try_diagnose_and_fix.py` |
| Approval gate | Simulated auto-approve with logging | Real approval would need Slack/web UI integration |
| Visibility | `agent.iter()` for step-by-step output | Proven in `try_agent_loop.py` |
| Structured output | Pydantic models for alerts and reports | Proven in `try_structured.py` |
| Durable state | Not in scope for PoC v1 | Add pydantic-graph persistence in v2 |

---

## What This PoC Proves

If successful, this demonstrates that lightspeed-stack can:

1. **Run multiple agents** in a single deployment, each with different roles
2. **Detect issues autonomously** without user prompting
3. **Investigate and fix** using the agent loop with tools
4. **Collaborate between agents** — monitoring hands off to diagnostic with context
5. **Report structured results** that applications can consume programmatically
6. **Provide visibility** into what the agent is doing at each step

These are the capabilities that make lightspeed-stack a **cloud agent platform**, not just a chatbot proxy.

---

## What This PoC Does NOT Cover (Future Work)

| Capability | Why deferred | When to address |
|-----------|-------------|-----------------|
| Durable state / persistence | Needs pydantic-graph or DBOS integration | PoC v2 |
| Real Kubernetes events | Needs K8s client, real cluster access | Production integration |
| Real human approval flow | Needs Slack/web UI integration | When building production agents |
| Sandboxed execution | Unsolved architectural question | Needs a spike (LCORE TBD) |
| Agent configuration via YAML | Config schema not designed yet | After multi-agent architecture is validated |
| Skills integration | pydantic-ai-skills works but not wired into multi-agent | After LCORE-2076 lands |
| Predictive analytics | Needs real time-series data and ML models | Long-term |

---

## Files to Create

| File | Description |
|------|-------------|
| `playground/try_server_agents.py` | Main PoC: monitoring + diagnostic + conversational agents with collaboration |

### Builds on existing PoC files

| Existing file | What it proved | What we reuse |
|--------------|---------------|---------------|
| `try_diagnose_and_fix.py` | Diagnostic workflow with tools, verification, quality gate | Cluster state, diagnostic agent, tools |
| `try_multi_agent.py` | Agent delegation via `@agent.tool` | Delegation pattern |
| `try_agent_loop.py` | `agent.iter()` step visibility | Step-by-step output format |
| `try_structured.py` | Typed output models | `MonitoringAlert`, `DiagnosticReport` models |
