# Lightspeed-Stack Strategic Direction: Agent Harness & Pydantic AI

**Date**: 2026-06-17
**Source**: James Wong / JR Boos conversation (2026-06-17)
**Companion to**: `lcore-pydantic-ai-architecture.md`, `lcore-pydantic-ai-diagnose-and-fix.md`

---

## Core Question

What is lightspeed-stack's identity — a proxy to Llama Stack, a local agent harness (competing with Goose/Claude Code/OpenCode), or something else?

## Consensus: Lightspeed-Stack = Cloud Agents

Both JR and James converged on: **lightspeed-stack's value is as a cloud-based agent platform** — server-side, long-running, configurable agents deployed into customer environments (OpenShift, OpenStack, AAP clusters). Not a local CLI tool, not a Goose replacement, not a coding agent.

### What "cloud agents" means concretely

- Agents run in pods within the customer's cluster, not on user laptops
- Long-running autonomous agents (monitoring, housekeeping, anomaly detection)
- Conversational agents accessible via chat UI (the existing Lightspeed experience)
- Multiple agents per deployment, configurable per customer use case
- Access to cluster internals (via MCP, APIs, tools) that local agents can't reach

### What lightspeed-stack is NOT

- Not a local agent harness (that's Goose, Claude Code, OpenCode, OpenClaw)
- Not a proxy/passthrough to Llama Stack or OpenAI
- Not competing with coding agents

---

## The `/responses` API Problem

**JR's position (strongly held):** Exposing `/responses` was a mistake.

### Why it's problematic

1. **Makes LCS a proxy** — consumers (Goose, Developer Hub notebooks) bypass all LCS value (RAG, safety, quota, conversation management) and treat LCS as a dumb relay to Llama Stack
2. **Locks in Llama Stack** — if `/responses` must be maintained, Llama Stack can never be removed because LCS would have to reimplement the full OpenAI Responses API specification
3. **Maintenance burden** — as OpenAI adds features to the Responses API, LCS must keep up or fall behind
4. **Confusing architecture** — multiple response API layers (Goose → LCS `/responses` → Llama Stack `/responses` → OpenAI) with server-side tools at every layer

### What should happen instead

- `/query` (and `/streaming_query`) should be the primary API with all the "Red Hat Lightspeed flavor"
- If consumers need functionality missing from `/query`, add it to `/query` rather than pushing them to `/responses`
- Developer Hub notebooks switched to `/responses` because `/query` was missing features — that's a `/query` gap, not a reason to keep `/responses`

### Impact on Pydantic AI migration

- The current LCORE-2307 migration targets `/query` and `/streaming_query` — this aligns with the strategic direction
- `/responses` may not need a Pydantic AI migration if the plan is to deprecate it
- Moving to Pydantic AI makes `/query` more capable (skills, structured output, multi-agent), further reducing the need for `/responses`

---

## Multi-Agent: From Single to Many

### Current state

Lightspeed-stack is a **single agent** per deployment. JR described it as "overkill for supporting just a single agent."

### Target state

A single LCS deployment should expose **multiple configurable agents**:

| Agent type | Description | Example |
|-----------|-------------|---------|
| Conversational | Lives in the chat UI, responds to user questions | The current Lightspeed experience |
| Autonomous / monitoring | Runs continuously, watches metrics, triggers on anomalies | CPU spike detector, housekeeping agent |
| Diagnostic / RCA | Triggered on-demand, investigates and remediates | The diagnose-and-fix PoC pattern |
| Heuristic / predictive | Periodically checks patterns, predicts issues | Database connection issue predictor |

### Configuration vision

```yaml
# lightspeed-stack.yaml (future)
agents:
  - name: assistant
    type: conversational
    skills: [openshift-troubleshooting, code-review]
    model: granite-3.3

  - name: health-monitor
    type: autonomous
    trigger: cpu_utilization > 90%
    skills: [cluster-diagnostics]
    model: granite-3.3

  - name: rca-investigator
    type: diagnostic
    skills: [root-cause-analysis, openshift-troubleshooting]
    model: granite-3.3
```

Agents defined by product teams (Ansible, OpenShift, Developer Hub), not end users. End users interact through the chat UI or receive alerts from autonomous agents.

---

## Skills vs. Graph Workflows

### Two approaches discussed

**Approach 1: Deterministic graph workflows**
- Explicit nodes with prescribed steps: diagnose → recommend → approve → execute
- Human defines the workflow structure; each node uses specific skills/tools
- Predictable, testable, auditable
- What OpenShift Nexus is building (CRD-based orchestrator with graph nodes)

**Approach 2: Skill-driven agent autonomy**
- Hierarchical skills that the agent discovers and chains together on its own
- One "meta-skill" (e.g. HCC-upgrade) loads sub-skills as needed
- Claude Code-style: the agent figures out the planning from skill instructions
- More flexible, less predictable

### JR's suggestion

Build a PoC showing **both approaches side by side** for the same use case (e.g. OpenStack upgrade), comparing:
- Effectiveness of outcomes
- Predictability of execution path
- Token usage
- Developer effort to define the workflow

### JR's work in progress

JR is building a **Pydantic AI capability for dynamic workflow planning** — an agent that can use other configured agents and workflows to build plans dynamically. Expected by end of week (from 2026-06-17). This is the "agents building workflows" vision — AI creates the graph, not humans.

---

## OpenStack Use Cases

### Use case 1: Upgrade assistance (September 2026 deadline)

- OpenStack releasing version 19 in September
- Agent assists users through the upgrade journey
- Currently using OpenShift tooling (tech preview), looking to move to lightspeed-stack
- **Assessment:** For the local "can I upgrade?" check, users really just need OKP (knowledge base) + local CLI (oc commands). Lightspeed-stack adds value only if the upgrade agent runs server-side with access to cluster state.

### Use case 2: RCA / root cause analysis

- Something breaks → agent diagnoses the issue → recommends fix → human approves → agent executes
- 3-4 step workflow: diagnosis, recommendation, approval, execution
- **Assessment:** This is the cloud agent use case that fits lightspeed-stack well. Maps directly to the diagnose-and-fix PoC.

### The Goose architecture problem

Dan Prince's demo: Goose (local) → LCS `/responses` → Llama Stack → LLM

**Problems identified:**
- LCS becomes a proxy — adds latency but little value
- Goose handles the agent loop locally; LCS just provides RAG/knowledge
- For RAG only, users could connect Goose directly to OKP without LCS
- LCS can't execute commands on the user's laptop (it runs in a pod)
- Multiple layers of server-side tools create confusion

**Better architecture:**
- LCS runs as cloud agents inside the cluster with full access to cluster APIs
- Local agents (Goose, Claude Code) connect to LCS for knowledge/context but handle local tool execution themselves
- LCS is the brain (knowledge + reasoning), local agents are the hands (execution on user's machine)

---

## Sandboxing Gap

Both identified **sandboxing** as a key unsolved problem:

- Pydantic AI doesn't provide sandboxed code execution
- If agents generate scripts/playbooks, where do they run safely?
- JR proposed: LCS could provide server-side sandboxes — run generated commands in an isolated environment, verify they work, then approve for real execution
- This would be genuine value-add that local agents can't provide

---

## Action Items

| Item | Owner | Timeline |
|------|-------|----------|
| Clarify "agent harness" ticket scope with Stefan | James | 2026-06-18 |
| Feature readout sizing (S/M/L/XL) for agent harness | James | Before July readout |
| Share extended Pydantic AI demo repo | JR | ASAP |
| Dynamic workflow planning capability | JR | End of week (2026-06-20) |
| PoC: Skills-driven vs graph-based workflow comparison | James | TBD |
| Discuss `/responses` API future with team | James + JR | TBD |

---

## Open Questions

1. **What does Stefan actually want for the agent harness ticket?** James meeting him 2026-06-18.
2. **Should `/responses` be deprecated?** JR says yes; need team alignment.
3. **Multi-agent configuration format?** YAML-based agent definitions, but schema TBD.
4. **Sandboxing approach?** No solution proposed yet — needs a spike.
5. **Skills vs. graphs?** Need PoC comparison to decide the default workflow model.
6. **Can Pydantic AI support all of this?** The PoCs prove the core patterns work. The remaining questions are architectural (multi-agent config, sandboxing), not framework limitations.
