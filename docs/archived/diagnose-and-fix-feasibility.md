# Pydantic AI for Diagnose-and-Fix Workflows: Feasibility Assessment

**Date**: 2026-06-17 (updated)
**Companion to**: `lcore-pydantic-ai-architecture.md`
**PoC**: `lightspeed-stack/playground/try_diagnose_and_fix.py`

---

## Goal

Evaluate whether Pydantic AI is a practical framework for building autonomous diagnostic and remediation agents in lightspeed-stack — the "diagnose this cluster and fix what you can" pattern.

## What the Workflow Requires

```
1. DISCOVER  — scan hosts, services, logs → find anomalies
2. DIAGNOSE  — correlate findings, identify root causes
3. PLAN      — decide what to fix, in what order
4. ACT       — run remediation (restart service, clear disk, rollback deploy)
5. VERIFY    — check if the fix worked
6. ITERATE   — if not fixed, try something else
7. REPORT    — summarize what was done, what's still broken
```

This is not a single LLM call. It's a long-running, multi-step, stateful workflow with real-world side effects and a self-correcting loop.

## PoC Results

The PoC (`playground/try_diagnose_and_fix.py`) simulates a cluster with two broken hosts:
- **web-02**: degraded — app crashed after deploy v2.3.1 (CPU 92%, memory 88%)
- **db-01**: critical — disk at 95%, slow queries

The agent autonomously executed the full workflow in **12 tool calls**:

```
step 1  [tool] list_hosts                          ← DISCOVER
step 2  [tool] get_alerts
step 3  [tool] get_recent_deploys
step 4  [tool] check_host(web-01)                  ← DIAGNOSE
step 5  [tool] check_host(web-02)
step 6  [tool] check_host(db-01)
step 7  [tool] run_remediation(rollback web-02)    ← ACT (with approval gate)
step 8  [tool] run_remediation(cleanup_disk db-01)
step 9  [tool] check_host(web-02)                  ← VERIFY
step 10 [tool] check_host(db-01)
step 11 [tool] get_alerts                          ← CONFIRM
step 12 [tool] final_result(DiagnosticReport)      ← REPORT
```

**Cluster state actually mutated:**
- web-02: degraded (cpu=92%) → healthy (cpu=40%) after rollback
- db-01: critical (disk=95%) → healthy (disk=65%) after cleanup
- web-01: healthy throughout (no action needed)

The agent returned a structured `DiagnosticReport` (Pydantic model) with issues found, actions taken, and cluster health status.

## What Pydantic AI Handles Well

### Multi-step tool loop (core loop)

`Agent.run()` / `agent.iter()` handles the discover → diagnose → act → verify loop natively. The agent keeps calling tools and reasoning until it decides it has enough information or has completed remediation. No custom loop code needed — the framework manages it.

### Self-verification

The agent re-checked each host after remediation (steps 9-10) without being explicitly told to for each host. The instructions said "verify every remediation" and the agent followed through. The tool results showing `status: healthy` let it confirm success.

### Quality gate via `output_validator`

```python
@diag_agent.output_validator
async def verify_all_fixed(ctx, report: DiagnosticReport) -> DiagnosticReport:
    unhealthy = [name for name, h in cluster_state["hosts"].items()
                 if h["status"] != "healthy"]
    if unhealthy and report.cluster_healthy:
        raise ModelRetry(f"Hosts still unhealthy: {unhealthy}. Fix them.")
    if not report.actions_taken:
        raise ModelRetry("No actions taken. Use run_remediation tool.")
    return report
```

`ModelRetry` is a built-in Pydantic AI exception. When raised, the framework sends the feedback message back to the LLM and the agent tries again. This is a **programmatic quality gate** — even if the LLM wants to stop, the validator can force another iteration. Up to `retries` attempts.

### Human-in-the-loop approval (simulated)

The `run_remediation` tool includes a simulated approval gate:
```
[approval] Agent requests: rollback_deploy:frontend on web-02
           Reason: App process crashed after deploy v2.3.1
           -> AUTO-APPROVED (simulated)
```

In production, this would be an async approval flow (Slack, web UI, CLI prompt). If rejected, the tool raises `ModelRetry("User rejected. Try a different approach.")` and the agent tries something else.

### Structured output

The final report is a validated Pydantic model (`DiagnosticReport`), not free text. The application can programmatically inspect `report.cluster_healthy`, `report.actions_taken`, etc. The framework retries if the LLM output doesn't match the schema.

### Step-by-step visibility

`agent.iter()` exposes every node in the agent graph as it happens — `UserPromptNode`, `CallToolsNode` (with `ToolCallPart`), `ModelRequestNode` (with `ToolReturnPart`). This enables real-time UI updates showing the agent's progress through the diagnostic workflow.

## Gaps Found

### ~~1. State persistence / checkpointing~~ — SOLVED

~~Pydantic AI agents are ephemeral.~~

**Update (2026-06-17):** This gap is addressed through two mechanisms:

**a) pydantic-graph built-in state persistence**

The `pydantic-graph` library (already installed — it powers `agent.iter()`) has built-in state snapshotting that allows graph runs to be interrupted and resumed from any node:

- `FileStatePersistence` — saves snapshots to a JSON file
- `FullStatePersistence` — in-memory, holds full snapshot history
- `BaseStatePersistence` — abstract base class for custom backends (PostgreSQL, Redis, etc.)

A diagnostic workflow can checkpoint after each tool round. If the process dies, it resumes from the last completed node.

**b) Durable execution integrations (officially supported)**

Pydantic AI has official integrations with two durable execution runtimes:

- **[DBOS](https://pydantic.dev/docs/ai/integrations/durable_execution/overview/)** — wrap `agent.run()` with `DBOSAgent(agent)`. Each LLM call and tool invocation is automatically checkpointed to a database. On crash, the workflow resumes from the last completed step. One line of code to add.

- **[Temporal](https://temporal.io/blog/build-durable-ai-agents-pydantic-ai-and-temporal)** — native Temporal support built into Pydantic AI. Replay-based fault tolerance — the agent picks up exactly where it left off after crashes, deploys, or API timeouts.

**Revised assessment:** Persistence and checkpointing are production-ready, not gaps.

### ~~2. Human-in-the-loop~~ — IMPROVED

**Update (2026-06-17):** `pydantic-graph`'s state persistence directly enables async human-in-the-loop:

1. The graph runs until it hits an approval node
2. State is persisted (file, database, etc.)
3. The process can exit — the workflow is saved
4. When the human approves (via Slack, web UI, etc.), the graph resumes from the persisted state

This is closer to LangGraph's `interrupt_before` than the "build it as a tool" workaround described previously. You still write the approval node yourself, but the interrupt/resume mechanism is built in.

The synchronous tool-based approach from the PoC still works for simpler cases (operator watching in real-time). The pydantic-graph approach adds async approval without blocking the agent loop.

**Comparison**: LangGraph's `interrupt_before` is slightly more ergonomic (declarative), but pydantic-graph achieves the same result through node-level persistence.

### 3. Context window management — PARTIAL

A real diagnostic session generates thousands of tokens of tool results (host status, logs, error messages). Pydantic AI has `history_processors` for trimming old messages, but no intelligent compaction.

**Workaround**: Use a `history_processor` that summarizes tool results beyond N rounds, keeping only the most recent results and a summary of earlier ones.

**Comparison**: Lightspeed-stack already has conversation compaction (`apply_compaction_blocking`). This could be adapted for the agent loop.

### 4. Workflow branching — IMPLICIT ONLY

The agent decides the flow implicitly based on tool results. There's no way to define "if disk issue → run disk remediation subgraph; if network issue → run network subgraph" as explicit branches.

For the PoC, this worked fine — the LLM correctly chose `rollback_deploy` for web-02 and `cleanup_disk` for db-01. But for complex workflows with strict ordering requirements, implicit branching is less predictable than an explicit graph.

**Comparison**: LangGraph has conditional edges (`if state.issue_type == "disk": goto disk_node`).

### 5. Timeout / cancellation — MINIMAL

No built-in timeout for the overall workflow. If the LLM enters an infinite tool-calling loop (unlikely but possible), the only limit is `retries` on the output validator. There's no "this workflow should complete within 10 minutes" constraint.

**Workaround**: Wrap `agent.run()` in `asyncio.wait_for()`.

## Framework Comparison for This Use Case

| Requirement | Pydantic AI | LangGraph | Raw Llama Stack |
|-------------|------------|-----------|-----------------|
| Multi-step tool loop | Built-in | Built-in (graph nodes) | Manual loop |
| Self-verification | Prompt-driven + `output_validator` | Explicit verification node | Manual |
| Human-in-the-loop | pydantic-graph interrupt/resume + tool-based | First-class (`interrupt_before`) | Manual |
| Structured output | Built-in with retry | Via Pydantic | No |
| State persistence | pydantic-graph (`FileStatePersistence`, custom) | Built-in checkpointing | Manual |
| Durable execution | DBOS, Temporal (official integrations) | Built-in checkpointing | Manual |
| Workflow branching | Implicit (LLM decides) or pydantic-graph edges | Explicit (conditional edges) | Manual |
| Context management | `history_processors` | Manual | Conversation compaction exists |
| MCP tool access | Native `MCPToolset` | Via LangChain tools | Native |
| Skills | `pydantic-ai-skills` | No equivalent | Custom (LCORE-2071) |
| Type safety | Best-in-class | Good | Minimal |
| Complexity | Low-medium | Medium-high | Low |

## Assessment

**Updated 2026-06-17 after discovering pydantic-graph persistence and durable execution integrations.**

**Pydantic AI is practical for this use case.** The original "with caveats" qualifier has weakened — the two biggest gaps (persistence and human-in-the-loop) are now addressed by built-in features.

### Where it works well

- **Single-session diagnostics** (5-15 minutes, operator watching) — the PoC proves this works end-to-end
- **Tool-driven workflows** where the LLM decides the flow — discovery, diagnosis, remediation, verification
- **Quality gates** via `output_validator` + `ModelRetry` — the agent can't "give up" until the validator is satisfied
- **Structured reporting** — the final output is a validated Pydantic model, not free text
- **Step visibility** — `agent.iter()` gives real-time progress for UI integration
- **Long-running workflows** — pydantic-graph state persistence + DBOS/Temporal for crash recovery
- **Human approval** — pydantic-graph interrupt/resume for async approval flows; tool-based `ModelRetry` for synchronous approval

### Remaining gaps (minor)

- **Complex multi-path workflows** — if the diagnostic tree has strict ordering or conditional branches, LangGraph's explicit graph is more ergonomic. Pydantic AI can do this via pydantic-graph nodes with explicit edges, but it's more manual than LangGraph's declarative approach.
- **Context window management** — `history_processors` exist but no intelligent compaction. Lightspeed-stack's `apply_compaction_blocking` could be adapted.
- **Timeout / cancellation** — no built-in workflow timeout. Use `asyncio.wait_for()`.

### Where LangGraph still has an edge

- **Declarative graph definition** — LangGraph's conditional edges and graph visualization are more ergonomic for complex, predetermined workflows
- **Ecosystem maturity for orchestration** — LangGraph is purpose-built for stateful agent orchestration; Pydantic AI added these capabilities later

### Recommendation

For lightspeed-stack's near-to-medium term:

1. **Use Pydantic AI** for the diagnose-and-fix pattern. The core loop, persistence, and human-in-the-loop are all covered. The team already has provider, bridge, and skills infrastructure built on Pydantic AI.

2. **For durability, choose based on infrastructure:**
   - **DBOS** if you want minimal code change (`DBOSAgent(agent)` wrapper) and PostgreSQL-based checkpointing
   - **Temporal** if the organization already runs Temporal for other workflows
   - **pydantic-graph `FileStatePersistence`** for simpler deployments or development/testing

3. **Use pydantic-graph for human-in-the-loop** — define an approval node that persists state and resumes after human input. This is cleaner than the tool-based `ModelRetry` approach for async approval flows.

4. **Keep the framework abstraction option open** — per the appendix in `lcore-pydantic-ai-architecture.md`, introducing an `AgentFramework` ABC before the endpoint swaps land would make a future framework switch possible without rewriting endpoints.

## Durable Execution Options

*Research added 2026-06-17.*

Pydantic AI provides three paths to durable workflows, each at a different level of the stack:

### Option 1: pydantic-graph state persistence (built-in)

The `pydantic-graph` library provides graph-level state snapshotting. Before and after each node runs, the graph state is persisted.

```python
from pydantic_graph.persistence import FileStatePersistence

persistence = FileStatePersistence("diagnostic_run.json")
async with agent.iter("Diagnose the cluster", persistence=persistence) as run:
    async for node in run:
        # State is automatically saved after each node
        ...
```

**Built-in backends:**
- `SimpleStatePersistence` — in-memory, latest snapshot only (default)
- `FullStatePersistence` — in-memory, full snapshot history
- `FileStatePersistence` — JSON file on disk
- `BaseStatePersistence` — abstract base for custom backends (PostgreSQL, Redis)

**Best for:** Development, testing, and simpler deployments where process crashes are infrequent. Requires custom code to resume from a persisted file.

### Option 2: DBOS (official integration)

Wraps `agent.run()` as a durable DBOS workflow. Every LLM call and tool invocation is automatically checkpointed to PostgreSQL. Zero manual serialization.

```python
from dbos import DBOSAgent

durable_agent = DBOSAgent(diag_agent)
result = await durable_agent.run("Diagnose the cluster")
# If the process crashes here, it resumes from the last completed step on restart
```

**Best for:** Production deployments where you want automatic checkpointing with minimal code change. Requires PostgreSQL.

### Option 3: Temporal (official integration)

Native Temporal support built into Pydantic AI. Uses replay-based fault tolerance — the agent replays completed steps from Temporal's event history and continues from where it left off.

**Best for:** Organizations already running Temporal infrastructure. Most robust option for long-running workflows (hours/days) with complex failure modes.

### Comparison

| Aspect | pydantic-graph | DBOS | Temporal |
|--------|---------------|------|----------|
| Code change | Use `agent.iter()` + persistence param | `DBOSAgent(agent)` wrapper | Temporal workflow/activity decorators |
| Storage | File, memory, or custom | PostgreSQL | Temporal server |
| Granularity | Per graph node | Per LLM call / tool invocation | Per activity |
| Resume after crash | Manual (load file, resume graph) | Automatic on restart | Automatic via replay |
| Infrastructure | None | PostgreSQL | Temporal server + workers |
| MCP support | Yes | Yes | Yes |
| Streaming support | Yes (via `agent.iter()`) | Yes | Yes |
| Complexity | Low | Low-medium | Medium-high |

### Recommendation for lightspeed-stack

**Start with pydantic-graph `FileStatePersistence`** for development and PoC. When moving to production, evaluate **DBOS** (simplest production path — one wrapper, PostgreSQL-backed) vs **Temporal** (most robust, but requires Temporal infrastructure). The choice depends on what the ops team already runs.

---

## Related Tickets

| Ticket | Relevance |
|--------|-----------|
| LCORE-2281 | Durable agents (long-running tasks) — directly requires persistence/checkpointing |
| LCORE-2307 | Endpoint migration to Pydantic AI — foundation for agentic workflows |
| LCORE-2076 | Wire skills into request flow — skills become available to diagnostic agents |
| LCORE-1339 | Agent Skills feature — diagnostic skills (e.g. openshift-troubleshooting) feed into this pattern |

## PoC Files

| File | Description |
|------|-------------|
| `playground/try_diagnose_and_fix.py` | Full diagnose-and-fix workflow with simulated cluster, approval gates, verification loop, structured report, and `output_validator` quality gate |
| `playground/try_agent_loop.py` | Simpler agent loop with infrastructure tools and `agent.iter()` step visibility |
| `playground/try_multi_agent.py` | Multi-agent delegation pattern — router + specialists |
