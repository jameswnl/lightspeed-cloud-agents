# Pydantic AI in Lightspeed-Stack: Architecture & Migration Map

**Date**: 2026-06-09 (updated)
**Companion to**: `lcore-pydantic-analysis.md` (deep dive on provider/transport internals)

---

## How Pydantic AI Fits In

Pydantic AI is being integrated as an **agent orchestration layer** between lightspeed-stack's FastAPI endpoints and Llama Stack's LLM backend. It does not replace Llama Stack — it wraps it. Llama Stack continues to handle LLM inference, RAG, safety, and storage. Pydantic AI adds agent-level primitives on top.

This aligns with the OGX/Llama-Stack announcement (referenced in LCORE-2069): OGX is the server-side API layer, **not** an agent framework. Pydantic AI fills the agent framework role that OGX explicitly leaves to external libraries.

### Current Architecture

```
User Request
  → FastAPI endpoint (query.py / responses.py / streaming_query.py / a2a.py)
    → prepare_responses_params()  →  ResponsesApiParams
      → client.responses.create(**params)     ← direct Llama Stack Responses API
        → Llama Stack (server mode via HTTP, or library mode in-process)
          → LLM inference
```

All RAG, MCP tools, moderation, and conversation management are configured upstream in `ResponsesApiParams` and passed directly to Llama Stack.

### Target Architecture

```
User Request
  → FastAPI endpoint
    → prepare_responses_params()  →  ResponsesApiParams  (same as today)
      → build_agent(client, params)           ← Pydantic AI bridge
        → Agent.run(input) / Agent.run_stream(input)
          → LlamaStackProvider                ← Pydantic AI provider
            → Llama Stack (server or library)  (same backend)
```

The bridge reuses the **same** Llama Stack client and parameters — same auth, same model routing, same MCP tools, same conversation tracking. The HTTP contract and response shapes are unchanged from a client perspective (LCORE-2307 acceptance criteria).

### Two-Layer Design

**Layer 1: Provider** (`src/pydantic_ai_lightspeed/llamastack/`)
- `LlamaStackProvider` — implements `pydantic_ai.providers.Provider[AsyncOpenAI]`
- `LlamaStackLibraryTransport` — custom httpx transport for in-process library mode
- Dual-mode: server (HTTP to Llama Stack at `localhost:8321/v1`) or library (in-process via `AsyncLlamaStackAsLibraryClient`)
- Status: **Closed** (LCORE-2308, PR #1806)

**Layer 2: Bridge** (`src/utils/pydantic_ai.py`)
- `build_agent(client, responses_params)` → constructs a `Pydantic AI Agent[None, str]`
- Single entry point used by both streaming and non-streaming paths
- Maps `ResponsesApiParams` → `OpenAIResponsesModelSettings`
- Passes Llama-Stack-specific fields (conversation, tools, safety) via `extra_body`
- Status: **Merged** (LCORE-2309, PR #1817)

---

## Endpoint Migration Targets

There are 6 call sites that do `client.responses.create()` today. The initial migration targets `/query` and `/streaming_query` (LCORE-2307 epic scope):

| Endpoint | File | Streaming | Complexity | Migration Ticket |
|----------|------|-----------|------------|-----------------|
| `/query` | `query.py` | No | RAG + MCP + moderation + quota | **LCORE-2310** |
| `/streaming_query` | `streaming_query.py` | Yes | Streaming + context mgmt | **LCORE-2311** |
| `/responses` | `responses.py` | Yes (SSE) | Full streaming pipeline + moderation + turn persistence | — (future) |
| `/responses` (tools) | `responses.py` | Yes | Tool-call loop | — (future) |
| `/a2a` | `a2a.py` | Yes | A2A task state machine | — (future) |
| `/rlsapi/v1` | `rlsapi_v1.py` | No | Simplest — just input + model + instructions | — (future) |

### LCORE-2310: `/query` swap

- **Function**: `retrieve_response()` in `query.py` — same signature and `TurnSummary` return type preserved
- **Swap**: `client.responses.create(**responses_params.model_dump())` → `agent.run()`
- **Contracts preserved**: moderation early-return, context-length/API error handling, `build_turn_summary` inputs
- **Response parity**: `QueryResponse` fields (tokens, tool_calls, tool_results, rag_chunks, referenced_documents) remain populated equivalently
- **Assignee**: Andrej Simurka

### LCORE-2311: `/streaming_query` swap

- **Function**: `retrieve_response_generator()` in `streaming_query.py` — same `(AsyncIterator[str], TurnSummary)` tuple return preserved
- **Swap**: `client.responses.create()` → `agent.run_stream()`
- **Contracts preserved**: shield-blocked path (still uses `shield_violation_generator`), SSE/event stream format unchanged
- **Critical requirement** (from LCORE-2307): stream interrupt path (`get_stream_interrupt_registry`, `_register_interrupt_callback`, `generate_response`) must still cancel and persist interrupted turns
- **Assignee**: Andrej Simurka

### Note on RAG/MCP

RAG context and MCP tools are injected **upstream** in `prepare_responses_params()`, not by Pydantic AI. The swap only changes the LLM call mechanism, not the tool/RAG plumbing. Skills integration into the Pydantic AI agent layer is a separate future step (LCORE-2076).

---

## Can Pydantic AI Replace Llama Stack?

*Research conducted 2026-06-04. Pydantic AI capabilities have evolved significantly since the original feasibility analysis.*

### MCP Tool Calling — Yes, Pydantic AI handles this natively

Pydantic AI now has **first-class, built-in MCP client support** (not a third-party wrapper):

- `MCPToolset` (recommended) — built on FastMCP Client, supports full MCP protocol: tools, resources, sampling, elicitation, OAuth
- Three transports: `MCPServerStdio`, `MCPServerStreamableHTTP`, `MCPServerSSE`
- Register MCP servers via `toolsets=[server]` on the Agent
- Tool prefix for conflict prevention across multiple MCP servers
- Human-in-the-loop tool approval built in

The original analysis rated MCP feasibility at ~60%. **Updated assessment: 100%.** Pydantic AI can own MCP tool calling directly, and this is a concrete near-term improvement — MCP tools could move from Llama Stack's tool runtime to Pydantic AI's native MCP layer without dropping Llama Stack for anything else.

### RAG — Possible but costly, architecturally different

Pydantic AI has **no built-in RAG abstraction** — no vector store management, no document ingestion, no chunking. Their docs say: "Function tools are basically the 'R' of RAG."

The Pydantic AI RAG pattern is **agentic RAG**:
1. Write a `@agent.tool` that queries your vector store (pgvector, MongoDB, Solr, etc.)
2. Register it with the agent
3. The LLM decides when to retrieve

This differs from lightspeed-stack's current **pre-retrieval** pattern where `build_rag_context()` runs *before* the LLM call and injects context into the prompt. Both work, but the tradeoffs differ:

| Aspect | Current (pre-retrieval via Llama Stack) | Agentic RAG (via Pydantic AI tools) |
|--------|----------------------------------------|-------------------------------------|
| When retrieval happens | Before LLM call, deterministically | During LLM call, LLM decides |
| RAG context in prompt | Always present | Only when LLM asks for it |
| Vector store management | Llama Stack Memory API (register, insert, query, delete) | Custom tool code, manage yourself |
| BYOK RAG config | YAML-driven, `enrich_byok_rag()` | Rewrite as tool registration |
| Solr integration (OKP) | Built into `enrich_solr()` | Rewrite as Pydantic AI tool |
| Retrieval quality control | Deterministic, same chunks every time | LLM-dependent, may vary |

**Updated assessment: ~50% feasible** (up from 25%). Pydantic AI's tool system and embeddings support make it technically possible, but migrating BYOK RAG + Solr + vector store lifecycle would be significant custom work. The pre-retrieval vs. agentic-retrieval architectural difference also needs a deliberate decision.

### Safety / Shields / Guardrails — Not in Pydantic AI core

Pydantic AI has **no built-in safety layer**. Community packages exist but are not Pydantic-maintained:

- **pydantic-ai-shields** (vstorm) — 10 capabilities: CostTracking, ToolGuard, InputGuard, OutputGuard, AsyncGuardrail, PromptInjection, PiiDetector, SecretRedaction, BlockedKeywords, NoRefusals
- **pydantic-ai-guardrails** (jagreehal) — multi-layered: native guardrails, llm-guard ML scanners, autoevals LLM-powered quality checks

What lightspeed-stack uses from Llama Stack's safety:
- `run_shield_moderation()` before every LLM call
- Llama Guard / Prompt Guard integration
- Configurable violation levels (INFO, WARN, ERROR)
- Input/output moderation integrated into the request flow

**Updated assessment: ~30% feasible** (up from 20%). The community packages cover some ground, but they're not enterprise-grade replacements for Llama Stack's Safety API with Llama Guard. Rebuilding the moderation pipeline would be high-effort and high-risk.

### Conversation / Memory — Basic in Pydantic AI

- `message_history` parameter for multi-turn within a session
- JSON serialization via `ModelMessagesTypeAdapter` for persistence
- `history_processors` for trimming, summarization, privacy filtering
- **No managed storage** — you serialize/deserialize to your own store
- No equivalent to Llama Stack's Memory API (vector, KV, keyword, graph backends)

**Assessment: ~40% feasible.** Lightspeed-stack already has SQLAlchemy-based conversation storage, so the persistence layer exists independently. But Llama Stack's conversation tracking (via the Responses API `store` and `conversation` params) does heavy lifting that would need reimplementation.

### LLM Provider Abstraction — Not replaceable

Llama Stack's provider registry lets lightspeed-stack swap between Azure, Vertex AI, local Ollama, etc. via config YAML — no code changes. Pydantic AI supports multiple providers (OpenAI, Anthropic, Gemini, Ollama) but has no equivalent registry/plugin architecture for runtime provider swapping.

**Assessment: Not feasible.** This is core infrastructure, not an agent concern.

### Verdict: Don't drop Llama Stack, but shift MCP ownership

```
                        Today                          Recommended Direction
                     ┌──────────┐                     ┌──────────┐
                     │Pydantic  │                     │Pydantic  │
                     │AI        │                     │AI        │
                     │          │                     │ + MCP ◄──── move MCP here
                     │(agent    │                     │ + Skills     (native support)
                     │ only)    │                     │(agent +  │
                     └────┬─────┘                     │ tools)   │
                          │                           └────┬─────┘
                     ┌────▼─────┐                     ┌────▼─────┐
                     │Llama     │                     │Llama     │
                     │Stack     │                     │Stack     │
                     │ + RAG    │                     │ + RAG    │  keep these on
                     │ + Safety │                     │ + Safety │  Llama Stack
                     │ + MCP ◄──── currently here     │ + Memory │
                     │ + Memory │                     │ + Providers│
                     │ + Providers│                    └──────────┘
                     └──────────┘
```

**Pydantic AI on top of Llama Stack** remains the right architecture. The near-term opportunity is moving **MCP tool calling** to Pydantic AI's native layer, which is now mature. RAG, safety, memory, and provider management stay on Llama Stack.

---

## JIRA Hierarchy

### Full Hierarchy (Feature → Epic → Story)

```
LCORE-2069: Introduce Agentic AI Library into Lightspeed Core
│   Type: Feature | Status: In Progress | Priority: Blocker
│   Assignee: Anik Bhattacharjee
│
├── LCORE-2070: Agentic AI Library (Epic) ✅ Closed
│   │   Research & decision phase — Pydantic AI selected
│   │
│   ├── LCORE-2068: [SPIKE] Agentic AI Library 1                  ✅ Closed
│   ├── LCORE-2081: [SPIKE] Agentic AI Library 2                  ✅ Closed
│   ├── LCORE-2163: [SPIKE] Agentic AI Library Migration          ✅ Closed
│   └── LCORE-2124: Write up decision communication doc            ✅ Closed
│
└── LCORE-2307: Migrate /query and /streaming_query (Epic) 🔧 In Progress
    │   Implementation phase — swap endpoints to Pydantic AI
    │
    ├── LCORE-2308: Llama Stack Pydantic AI provider               ✅ Closed  (PR #1806)
    ├── LCORE-2309: Bridge ResponsesApiParams → build_agent()      ✅ Closed  (PR #1817)
    ├── LCORE-2310: /query — swap for agent.run                    🔧 In Progress
    └── LCORE-2311: /streaming_query — swap for agent.run_stream   🔧 In Progress


LCORE-1339: Support for Agent Skills in Lightspeed Core
│   Type: Feature | Status: In Progress
│   Assignee: JR Boos
│
└── LCORE-2071: Implement agent skills (Epic) 🔧 In Progress
    │
    ├── LCORE-2072: Add skill configuration model                  ✅ Closed
    ├── LCORE-2073: Implement list_skills tool                     ✅ Closed
    ├── LCORE-2074: Implement activate_skill tool                  ✅ Closed
    ├── LCORE-2075: Implement load_skill_resource tool             ✅ Closed
    ├── LCORE-2076: Wire skill tools into request flow             📋 New
    ├── LCORE-2077: Document Agent Skills feature                  ✅ Closed
    ├── LCORE-2078: Add integration tests for skills               📋 New
    ├── LCORE-2079: Add E2E feature file for skills                ✅ Closed
    └── LCORE-2080: Implement E2E step definitions for skills      📋 New
```

### Feature-Level Relationships

```
OCPSTRAT-2903: [OLS] Skills & Guided Troubleshooting (Closed)
  │
  │ informs
  ▼
LCORE-2069: Introduce Agentic AI Library ─── blocks ──→ LCORE-1339: Agent Skills
  (Pydantic AI as the framework)                            │
                                                            ├── blocks → LCORE-2253: Migration blockers — Ask Red Hat
                                                            ├── blocks → LCORE-2438: Migration blockers — Red Hat AI (navigator)
                                                            └── blocks → RHDHPLAN-1324: Skills, Rules, Resources for reasoning
```

LCORE-2069 (Pydantic AI) **must land before** LCORE-1339 (Agent Skills), which in turn **blocks** three downstream consumer teams.

### Epic-Level Relationships

```
LCORE-2070: Agentic AI Library ✅               LCORE-2071: Implement Agent Skills 🔧
  (research — which library?)                     (implementation — skills on Pydantic AI)
  Parent: LCORE-2069                              Parent: LCORE-1339
  Sequencing: completed first                     Sequencing: blocked until 2307 stories land

LCORE-2307: Migrate /query & /streaming_query 🔧
  (implementation — swap endpoints)
  Parent: LCORE-2069
  Sequencing: in progress, follows 2070
  Stories: 2308 ✅ → 2309 ✅ → 2310 🔧 / 2311 🔧
```

The two epics under LCORE-2069 are sequential: **2070** (closed) answered "which library?" → Pydantic AI. **2307** (in progress) is executing the endpoint migration. Once 2307's stories land, **2071** under the skills feature can wire skills into the Pydantic AI agent layer (specifically LCORE-2076: "Wire skill tools into request flow").

### Related Tickets

| Ticket | Summary | Status | Relationship |
|--------|---------|--------|-------------|
| LCORE-1976 | Agent Azure Token Refresh Support | New | Will need Pydantic AI path support |
| LCORE-1974 | Agent Model/Provider Override Support | New | Will need Pydantic AI path support |
| LCORE-2281 | Durable agents (long-running tasks) | New | Future Pydantic AI capability |
| LCORE-2254 | Spike: LCORE compatibility with Agentic OLS | New | OLS integration with agentic stack |
| LCORE-2051 | Spike: Codex CLI Skill | New | Skill that will run on Pydantic AI |
| LCORE-2129 | Update skills for Tiger Team POC | In Progress | Near-term skills work |

---

## Key People

| Person | Handle | Tickets | Role |
|--------|--------|---------|------|
| JR Boos | `jrobertboos` | LCORE-2308, 2309, 1339 | Provider & bridge author, skills design lead |
| Andrej Simurka | `asimurka` | LCORE-2310, 2311 | Endpoint swap implementer, streaming models |
| Anik Bhattacharjee | `anik120` | LCORE-2069, 2072 | Agentic library feature owner, skills config model |
| Maxim Svistunov | `max-svistunov` | LCORE-836 spike | Architecture research, Pydantic AI feasibility analysis |
| Pavel Tisnovsky | `tisnik` | — | Code review, merging, Pydantic validation work |

---

## Pydantic AI vs. Llama Stack: Capability Ownership

*Updated 2026-06-04 with current Pydantic AI capabilities research.*

| Concern | Current Owner | Can Pydantic AI Replace? | Updated Assessment | Notes |
|---------|--------------|--------------------------|-------------------|-------|
| Agent orchestration | **Pydantic AI** | — | Core value | Structured output, retries, tool calling |
| MCP tool calling | **Llama Stack** | **Yes** | 100% (was ~60%) | Pydantic AI has first-class built-in MCP support; recommend migrating |
| RAG / vector stores | **Llama Stack** | Partially | ~50% (was ~25%) | Possible via agent tools, but no managed stores; pre-retrieval vs agentic-retrieval is an arch decision |
| Safety / shields | **Llama Stack** | No | ~30% (was ~20%) | Community packages exist, not enterprise-grade; Llama Guard has no equivalent |
| Conversation memory | **Llama Stack** | Partially | ~40% | Basic history support; no managed storage backends |
| Provider registry | **Llama Stack** | No | Not feasible | Runtime provider swapping via config has no Pydantic AI equivalent |
| Multi-agent orchestration | **Pydantic AI** | — | Future capability | Not yet used in lightspeed-stack |
| Backend abstraction | **Pydantic AI** | — | Long-term goal | Swap Llama Stack without rewriting agent logic |

---

## Migration Strategy (Mapped to Tickets)

| Phase | Description | Tickets | Status |
|-------|-------------|---------|--------|
| 1. Research | Evaluate agentic libraries, select Pydantic AI | LCORE-2070 (epic), 2068/2081/2163 (spikes), 2124 (comms) | ✅ Done |
| 2. Provider | `LlamaStackProvider` wrapping Llama Stack | LCORE-2308 | ✅ Done |
| 3. Bridge | `build_agent()` from `ResponsesApiParams` | LCORE-2309 (PR #1817) | ✅ Done |
| 4. Endpoint swap | `/query` and `/streaming_query` use Pydantic AI | LCORE-2310, 2311 | 🔧 In Progress |
| 5. Skills integration | Agent Skills wire into Pydantic AI tool layer | LCORE-2076 | 📋 New |
| 6. MCP migration | Move MCP tools from Llama Stack runtime to Pydantic AI native MCP | — (not yet ticketed) | Proposed |
| 7. Backend abstraction | Config-level provider swap without rewriting agent logic | — | Future |

---

## Agent Skills Architecture (LCORE-2071)

*Detail from LCORE-2071 epic exploration, 2026-06-09.*

### How Skills Work

Skills follow a **tool-based progressive disclosure** pattern. Three LLM-callable tools form a pipeline:

1. **`list_skills`** — returns the skill catalog (name + description) so the LLM can decide which is relevant
2. **`activate_skill`** — loads the full SKILL.md body (instructions) for a chosen skill
3. **`load_skill_resource`** — reads individual files from a skill's `references/` subdirectory

Skills are discovered at startup by scanning configured directory paths for `SKILL.md` files with frontmatter (name, description). The repo ships two example skills: `openshift-troubleshooting` (with references/) and `code-review`.

### Configuration

`SkillsConfiguration` Pydantic model in `src/models/config.py` with a `paths: list[FilePath]` field. Users declare skill directories in `lightspeed-stack.yaml`. At startup the system scans paths, parses SKILL.md frontmatter, and validates skill name uniqueness.

### LCORE-2076: The Wiring Story

The critical remaining story — assigned to Andrej Simurka. Plans to use **`pydantic-ai-skills`** library (`SkillCapability`) to inject skills into each Pydantic AI Agent per request. Acceptance criteria include: correct tool routing, system prompt injection when skills are configured, deduplication of skill activations within a conversation, and preservation of skill content during conversation compaction.

This confirms: skills are the **first feature that requires the Pydantic AI layer**. You can't do `SkillCapability` with raw `client.responses.create()`.

### pydantic-ai-skills Library

[`pydantic-ai-skills`](https://github.com/DougTrajano/pydantic-ai-skills) (v0.11.0) implements the [agentskills.io](https://agentskills.io) standard for Pydantic AI:

- `SkillsCapability` (preferred) — bundles tools + instruction injection via Pydantic AI's Capability API
- `SkillsToolset` (alternative) — direct toolset integration
- Registers four tools: `list_skills`, `load_skill`, `read_skill_resource`, `run_skill_script`
- Compatible with the same SKILL.md format the repo already uses

---

## PoC Playground Results

*Hands-on validation conducted 2026-06-09 using `LlamaStackProvider` + Ollama (qwen3.6) and real OpenAI (gpt-4o-mini). All scripts at `lightspeed-stack/playground/`.*

### What Was Tested

| Script | Capability | Result |
|--------|-----------|--------|
| `try_pydantic_ai.py` | Basic chat, multi-turn, tool calling, structured output, streaming | All working |
| `try_skills.py` | Agent Skills with progressive disclosure | Working — agent calls `list_skills` → `load_skill` on demand |
| `try_mcp.py` | MCP via in-process FastMCP servers (todo + calculator) | Working — agent calls MCP tools, multi-server works |
| `try_multi_agent.py` | Delegation via `@agent.tool` + programmatic hand-off | Working — router delegates to specialists, token tracking unified |
| `try_structured.py` | Complex nested models + union output types | Working — agent returns validated Pydantic models with branching |
| `try_agent_loop.py` | Agentic loop with multi-step tool chaining + `agent.iter()` visibility | Working — agent autonomously chains 10+ tool calls; `iter()` exposes each step in real-time |

### Key Technical Findings

**Provider interchangeability confirmed.** Three providers produce the same results with the same Agent code:
- `LlamaStackProvider(base_url=OLLAMA_URL)` — JR's provider wrapping Ollama
- `OpenAIProvider(base_url=OLLAMA_URL)` — Pydantic AI's built-in provider pointing at Ollama
- `Agent("openai:gpt-4o-mini")` — real OpenAI, no custom provider needed

The only difference is one line (the provider constructor). This validates the backend abstraction goal.

**`defer_model_check=True` is required** for non-standard model names (e.g. `qwen3.6:latest`). Not needed for real OpenAI models.

**Provider goes on the Model, not the Agent.** The pattern is:
```python
provider = LlamaStackProvider(base_url=...)
model = OpenAIResponsesModel("model-name", provider=provider)
agent = Agent(model, defer_model_check=True)
```

**Skills progressive disclosure works.** The agent only loads skill instructions when the question matches a skill's domain. General questions ("What is 2+2?") are answered without touching skills. Domain questions trigger `list_skills` → `load_skill` → informed answer.

**MCP uses `MCPToolset`** (not the deprecated `FastMCPToolset`). In-process FastMCP servers work without spawning subprocesses — ideal for testing and embedding.

**Multi-agent delegation tracks tokens across agents.** `usage=ctx.usage` in the child agent call unifies token counting. The parent sees total consumption across the delegation chain.

**Union output types enable application-level branching.** `output_type=Union[Solution, NeedMoreInfo]` lets the agent signal whether it can answer or needs clarification. The application checks `isinstance()` and branches — no string parsing.

**Agentic loop runs automatically.** `Agent.run()` handles the tool-calling loop — the agent keeps calling tools and reasoning until it has enough information to answer. In the infrastructure diagnostic PoC, the agent autonomously chained 16 tool calls across 10 LLM round-trips to diagnose an issue.

**Streaming only covers the final answer by default.** `agent.run_stream()` buffers all intermediate tool-calling rounds and only streams the final text response. The user sees a pause during tool rounds, then the answer streams in. This matches Llama Stack's current behavior with `responses.create(stream=True)`.

**`agent.iter()` exposes every step in real-time.** For full visibility into the agentic loop, `agent.iter()` yields each node as it happens — `UserPromptNode`, `CallToolsNode` (with `ToolCallPart`), `ModelRequestNode` (with `ToolReturnPart`), and the final `TextPart`. This enables building UIs that show "calling list_hosts..." → "checking db-01..." → streaming answer.

### Benefits Validated by PoC

| Benefit | PoC Evidence | Why Pydantic AI is Required |
|---------|-------------|---------------------------|
| Agent Skills (progressive disclosure) | `try_skills.py` — skills loaded on demand | `SkillsCapability` on the Agent — no Responses API equivalent |
| Structured output + auto-retry | `try_structured.py` — complex nested models validated | `output_type=MyModel` forces schema compliance with retry |
| MCP native tool calling | `try_mcp.py` — in-process MCP servers | `MCPToolset` — richer than Llama Stack's tool runtime |
| Multi-agent composition | `try_multi_agent.py` — delegation + hand-off | Agent-to-agent delegation via tools — not possible with single `responses.create()` |
| Union output for branching | `try_structured.py` — Solution vs NeedMoreInfo | Application-level branching on typed output — no string parsing |
| Backend abstraction | `try_pydantic_ai.py` — three providers, same code | Swap provider in one line, agent logic untouched |
| Agentic loop + tool visibility | `try_agent_loop.py` — 16 tool calls, `iter()` step tracking | `Agent.run()` manages the loop; `iter()` exposes steps for UI |

---

## Dynamic Subagent Spawning: Claude Code vs. Pydantic AI

*Analysis from PoC exploration, 2026-06-10.*

### How Claude Code Does It

Claude Code's subagent spawning is a **harness feature**, not an LLM capability. The flow:

1. The system prompt tells the LLM which agent types are available
2. The LLM calls an `Agent` tool with a **custom prompt written on the fly**
3. The harness spawns a separate Claude session with its own context window
4. That session runs to completion and returns a result
5. The parent LLM continues with the result

The key insight: **agent types just control tool access** (e.g. `Explore` gets read-only tools, `Plan` gets no edit tools). The actual specialization comes from the prompt the parent LLM writes at spawn time. There are no pre-built "Jira agent" or "code review agent" personas — the parent constructs the right persona dynamically.

### How Pydantic AI Does It (Current)

Pydantic AI's multi-agent is more **static** — you pre-define specialist agents with fixed instructions, then wire them as tools on a router:

```python
# Pre-defined, fixed specialists
ansible_agent = Agent(model, instructions="You are an Ansible expert...")
openshift_agent = Agent(model, instructions="You are an OpenShift expert...")

@router.tool
async def ask_ansible_expert(ctx, question): ...
```

The router LLM picks from a known set of specialists. Adding a new specialty means adding a new agent and tool at the code level.

### Dynamic Spawning in Pydantic AI (Possible but Not Built-in)

You can achieve Claude Code-style dynamic spawning by giving the router a generic `delegate` tool:

```python
@router.tool
async def delegate(ctx: RunContext, task: str, instructions: str) -> str:
    """Spawn a sub-agent with custom instructions for any task."""
    sub_agent = Agent(
        make_model(),
        defer_model_check=True,
        instructions=instructions,  # LLM writes this on the fly
    )
    result = await sub_agent.run(task, usage=ctx.usage)
    return result.output
```

The router LLM constructs the specialist's persona at runtime: `delegate(task="diagnose disk on db-01", instructions="You are a Linux sysadmin...")`. This is more powerful than pre-defined specialists, but riskier — the LLM is writing prompts for other LLMs with no guardrails.

### Comparison

| Aspect | Claude Code | Pydantic AI (static) | Pydantic AI (dynamic) |
|--------|------------|---------------------|----------------------|
| Specialization | Prompt written on the fly | Fixed `instructions` per agent | LLM writes `instructions` at runtime |
| Adding new specialties | Just write a different prompt | Code change: new agent + tool | No code change — LLM improvises |
| Guardrails | Agent types limit tool access, sandboxing, permissions | Application code controls | None unless you build them |
| Tool access control | Per agent type (`Explore` = read-only, etc.) | Per agent (explicit tool registration) | Same tools for all sub-agents unless parameterized |
| Risk | Managed by harness | Low — fixed behavior | High — LLM prompt injection into sub-agents |

### Relevance to Lightspeed-Stack

For LCORE's near-term needs, **static multi-agent** is the right fit — the specialist agents (Ansible troubleshooting, code review, etc.) are well-defined and map to skills. Dynamic spawning is a future consideration if the platform needs to support open-ended, user-defined agentic workflows. The `delegate` pattern could be gated behind authorization controls to manage the risk.

---

## Appendix: Framework Abstraction — Making the Agent Layer Swappable

*Analysis conducted 2026-06-11. Optional architectural consideration — not currently planned.*

### Problem

The current Pydantic AI integration is spreading across multiple source files with direct imports of Pydantic AI types. If the team later wants to evaluate or adopt a different agent framework (LangChain, LangGraph, CrewAI, etc.), the coupling would require rewriting multiple files rather than swapping a plugin.

### Current Coupling Surface

```
File                                      Pydantic AI imports
──────────────────────────────────────    ─────────────────────────────────────
src/pydantic_ai_lightspeed/               Provider, ModelProfile, httpx transport
  (self-contained package)                → CLEAN: fully isolated

src/utils/pydantic_ai.py                  Agent, OpenAIResponsesModel,
  (bridge — build_agent)                    OpenAIResponsesModelSettings
                                          → CLEAN: single file, single function

src/utils/agents/tool_processor.py        ToolCallPart, ToolReturnPart,
  (~530 lines)                              NativeToolCallPart, NativeToolReturnPart,
                                            FileSearchTool, MCPServerTool, WebSearchTool
                                          → COUPLED: processes framework-specific
                                            message types into TurnSummary

src/models/common/agents/                 AgentRunResult
  turn_accumulator.py                     → COUPLED: framework type in models layer
```

**Endpoints: zero imports of Pydantic AI** (LCORE-2310/2311 not wired yet). This means the window to introduce an abstraction before coupling spreads to the endpoint layer is **now**.

### What's Clean (Easy to Swap)

- **Provider** (`src/pydantic_ai_lightspeed/`) — fully self-contained, only implements Pydantic AI's `Provider` interface. A LangChain equivalent would be a separate package.
- **Bridge** (`src/utils/pydantic_ai.py`) — single public function `build_agent()` taking framework-agnostic inputs (`AsyncLlamaStackClient`, `ResponsesApiParams`). A LangChain version would be `build_chain()` in a separate file.

### What's Coupled (Harder to Swap)

- **`tool_processor.py`** — the main problem. Directly imports and pattern-matches on Pydantic AI message types (`ToolCallPart`, `NativeToolReturnPart`, `MCPServerTool`, etc.) to convert them into lightspeed-stack's `TurnSummary`. A framework swap means rewriting this entire file.
- **`turn_accumulator.py`** — imports `AgentRunResult` from Pydantic AI. Minor, but it's in the shared models layer.

### Proposed Abstraction

```
src/
├── agent_frameworks/                    ← framework adapter layer
│   ├── base.py                          ← abstract interface
│   │     class AgentFramework(ABC):
│   │       async def run(params, input) -> AgentResult
│   │       async def run_stream(params, input) -> AsyncIterator[AgentEvent]
│   │
│   ├── pydantic_ai/                     ← current: Pydantic AI adapter
│   │     ├── provider.py                  LlamaStackProvider
│   │     ├── bridge.py                    build_agent()
│   │     └── event_mapper.py              maps Pydantic AI messages → AgentEvent
│   │
│   └── langchain/                       ← future: LangChain adapter
│         ├── chain_builder.py             build_chain()
│         └── event_mapper.py              maps LangChain events → AgentEvent
│
├── models/common/
│   └── agent_events.py                  ← framework-agnostic event types
│         AgentToolCall(name, args, id)
│         AgentToolResult(name, content, status, id)
│         AgentTextChunk(content)
│         AgentResult(output, usage, tool_calls, tool_results)
│
└── app/endpoints/
      query.py, responses.py            ← import AgentFramework only
```

The key piece is **`agent_events.py`** — framework-agnostic event types that the endpoints and `tool_processor` consume. Each framework adapter maps its native events to these common types. The endpoints never import `pydantic_ai` or `langchain` directly.

### Effort Estimate

| Change | Effort | Notes |
|--------|--------|-------|
| Extract `AgentFramework` ABC | Small | `run()` + `run_stream()` signature from current `build_agent` |
| Create `agent_events.py` | Medium | Cover: tool call, tool result, text chunk, native tools (MCP, file search, web search) |
| Rewrite `tool_processor.py` to consume agnostic events | Medium | ~530 lines, well-structured, mostly a type rename |
| Move `turn_accumulator.py` off `AgentRunResult` | Small | Replace with generic `AgentResult` |
| Wire endpoints to interface | Small | Endpoints don't import Pydantic AI yet — wire to ABC from day one |
| Config-driven framework selection | Small | `agent_framework: pydantic-ai` in `lightspeed-stack.yaml` |

### Timing

The window is **before LCORE-2310/2311 land** (the endpoint swaps). Once endpoints directly import `build_agent` and consume Pydantic AI types, the abstraction becomes a refactor instead of a design choice. Since those stories are In Progress but not merged, introducing the abstraction now means they'd wire to the interface from the start.

### Trade-offs

| For | Against |
|-----|---------|
| Swap frameworks without rewriting endpoints | Adds indirection before there's a second framework |
| Forces clean separation of concerns | YAGNI — LangChain swap may never happen |
| Endpoints stay framework-agnostic | Extra mapping layer has runtime cost (minimal) |
| Aligns with LCORE-2069's goal of "backend abstraction" | Team is already mid-flight on Pydantic AI stories |

### Recommendation

**Don't block current work for this.** But if the team is open to it, the minimal version is: introduce `AgentFramework` ABC + `agent_events.py` before LCORE-2310/2311 merge, and have the endpoint swap wire to the interface. The Pydantic AI adapter would be the only implementation. This costs ~1-2 days of upfront work but saves significant refactoring if a framework swap is ever needed.

---

## Sources

- [Pydantic AI MCP Overview](https://ai.pydantic.dev/mcp/overview/)
- [Pydantic AI MCP Client](https://ai.pydantic.dev/mcp/client/)
- [Pydantic AI RAG Example](https://ai.pydantic.dev/examples/rag/)
- [Pydantic AI Function Tools](https://ai.pydantic.dev/tools/)
- [Pydantic AI Messages & Chat History](https://pydantic.dev/docs/ai/core-concepts/message-history/)
- [pydantic-ai-shields (Community Guardrails)](https://github.com/vstorm-co/pydantic-ai-shields)
- [Llama Stack vs Pydantic AI Comparison](https://www.respan.ai/market-map/compare/llama-stack-vs-pydantic-ai)
- [Red Hat: Guardrails with Llama Stack](https://developers.redhat.com/articles/2026/05/04/guardrails-enterprise-safety-shields-llama-stack)
- [pydantic-ai-skills GitHub](https://github.com/DougTrajano/pydantic-ai-skills)
- [pydantic-ai-skills Docs](https://dougtrajano.github.io/pydantic-ai-skills/)
- [Agent Skills Specification](https://agentskills.io)
- [Pydantic AI Multi-Agent Applications](https://ai.pydantic.dev/multi-agent-applications/)
- [Pydantic AI Toolsets](https://ai.pydantic.dev/toolsets/)
- [Pydantic AI Capabilities](https://pydantic.dev/docs/ai/core-concepts/capabilities/)
