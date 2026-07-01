# Competitive Analysis: UnifAI vs lightspeed-cloud-agents

**Date**: 2026-07-01

## Overview

UnifAI is a production-grade, full-stack enterprise platform for building and running agentic AI workflows. It includes a React UI, RAG pipeline, Keycloak SSO, CLI, and Helm deployment — a different weight class from lightspeed-cloud-agents.

- **150K+ LOC**, 3,925 commits, 92 test files
- 4 microservices: Multi-Agent System, RAG Backend, Identity Service, Admin Config
- Infrastructure: MongoDB, Qdrant, RabbitMQ, Redis, Temporal, Keycloak
- Hosted under `redhat-community-ai-tools` GitHub org

## Where UnifAI is Ahead

| Area | UnifAI | lightspeed-cloud-agents |
|------|--------|------------------------|
| **UI** | Visual drag-and-drop blueprint builder (React Flow) | No UI — YAML + API only |
| **RAG** | Built-in document + Slack ingestion pipeline (Qdrant, Celery) | None |
| **Multi-tenancy** | Team ownership, edit locks, presence tracking, collaboration roles | PolicyFileAuthorizer (basic per-user RBAC) |
| **Auth** | Keycloak OAuth2/OIDC (production SSO) | Bearer token / K8s TokenReview |
| **Streaming** | NDJSON + Redis Streams, real-time node-by-node output | SSE polling (step-level only, token streaming deferred) |
| **Extensibility** | Auto-discovered plugin catalog (nodes, tools, LLMs, conditions) | Sandbox HTTP contract only |
| **Execution engines** | LangGraph (dev) + Temporal (prod), same blueprint format | Temporal only |
| **CLI** | Full-featured (browse blueprints, run sessions, OAuth login) | None |
| **Maturity** | 150K LOC, 92 test files, 3,925 commits | ~10K LOC, 400 tests |
| **Node types** | Custom agent, orchestrator, Claude agent, A2A delegation, deep reasoning, merger, branch chooser | Single agent step type + human-approval |

## Where We're Ahead or Differentiated

| Area | lightspeed-cloud-agents | UnifAI |
|------|------------------------|--------|
| **Approval gates** | First-class human-approval steps, auto-approve by risk level, approver identity in workflow state, pluggable notification channels | No explicit approval node type — workplan has "pending steps" but no approval UI or signal mechanism |
| **Ephemeral isolation** | Fresh sandbox container per step, securityContext (non-root, read-only fs), resource limits, destroyed after execution | In-process agent execution (LangGraph mode) — agents share the worker process |
| **Deployment simplicity** | Single binary + Temporal. Also runs on Podman (no K8s required) | Requires MongoDB + Qdrant + RabbitMQ + Keycloak + Redis — 5 infrastructure services |
| **Observability** | OTel distributed tracing, Prometheus metrics (ls_workflow_*), structured JSON audit events, health probes | Basic logging + MongoDB pipeline monitoring, no OpenTelemetry or distributed tracing |
| **Security hardening** | MCP secret allowlist, credential Secret volume mounts, fail-closed auth, pod securityContext, circuit breaker, cleanup failure metrics | Field-level hints (@SecretHint) but less container-level isolation |
| **Sandbox contract** | Any container implementing POST /v1/agent/run works as an agent step — framework-agnostic | Tightly coupled to LangChain/LangGraph ecosystem |
| **Podman support** | First-class deployment target with behavioral parity | K8s/OpenShift only |

## UnifAI Architecture

```
UI (React 18 + Vite)
  ↓
Nginx Router
  ├→ /api1 → RAG Backend (Flask + Celery + Qdrant)
  ├→ /api2 → Multi-Agent System (Flask + LangGraph/Temporal)
  ├→ /api3 → Identity Service (Keycloak OAuth2/OIDC)
  └→ /api4 → Admin Config Service (Flask + MongoDB)

Infrastructure: MongoDB, Qdrant, RabbitMQ, Redis, Temporal, Keycloak
```

### Key Architectural Patterns

- **Hexagonal architecture** — ports & adapters with clear domain separation
- **Auto-discovered element catalog** — nodes, tools, LLMs, conditions auto-registered from filesystem
- **Dual execution engines** — LangGraph (in-process, dev) and Temporal (distributed, prod) with same blueprint format
- **Blueprint-driven** — YAML definitions that are portable, versionable, and can be built visually in the UI
- **Celery task queue** — async document/Slack ingestion with RabbitMQ
- **Redis collaboration** — presence, edit locks, typing indicators, event streams

### Element Types

| Category | Examples |
|----------|---------|
| Nodes | custom_agent, orchestrator, claude_agent, a2a_agent, deep_agent, branch_chooser, llm_merger, mock_agent |
| Tools | ssh_exec, oc_exec, web_fetch, mcp_proxy, delegation, workplan |
| LLMs | OpenAI (any model), Gemini, Mock |
| Providers | MCP server client, A2A agent, RAG client |
| Retrievers | docs_rag (Qdrant), slack (channel/thread search) |
| Conditions | router_boolean, router_direct, threshold |

### Blueprint Format

```yaml
name: "Workflow Name"
llms:
  - rid: llm_1
    type: openai
    config: { model_name: gpt-4o }
nodes:
  - rid: agent_1
    type: custom_agent_node
    config:
      llm: llm_1
      tools: [ssh_exec, web_fetch]
      system_message: "..."
plan:
  - uid: input
    node: user_question_node
  - uid: agent
    after: input
    node: agent_1
  - uid: output
    after: agent
    node: final_answer_node
```

## What UnifAI Lacks (Our Strengths)

1. **No explicit approval gates** — workplan has pending steps but no pause/signal/approve mechanism. No approval identity capture or audit trail.
2. **No ephemeral container isolation** — LangGraph mode runs agents in-process. Temporal mode uses workers but not ephemeral per-step containers.
3. **No distributed tracing** — basic logging and MongoDB monitoring, no OTel or Jaeger.
4. **No Podman support** — K8s/OpenShift only.
5. **No cost tracking** — no token counting, spend tracking, or rate limiting.
6. **Heavy infrastructure** — minimum 5 services to deploy (MongoDB, Qdrant, RabbitMQ, Keycloak, Redis).

## Strategic Takeaways

### 1. Don't compete on breadth
UnifAI is a platform (UI + RAG + collaboration + admin). We're a workflow engine. Trying to match their feature breadth would take 6+ months and still be behind.

### 2. Double down on differentiators
- **Approval gates + governance** — our human-in-the-loop story is stronger
- **Ephemeral isolation + security hardening** — our security story is stronger
- **Deployment simplicity** — single binary + Temporal vs their 5-service stack
- **Podman support** — they can't run outside K8s
- **Framework-agnostic sandbox** — any container implementing the HTTP contract works

### 3. Address competitive gaps
- **T36 (streaming)** is a real gap — they stream node-by-node, we show nothing until step completion
- **T11 (agents-as-tools)** is our composability story — their visual builder is theirs
- **No UI** limits our audience to developers/operators. Consider whether a thin workflow status UI is worth building.

### 4. Positioning
**lightspeed-cloud-agents**: Secure, governed workflow execution for production AI agents. Approval gates, ephemeral isolation, dual deployment (K8s + Podman), simple to deploy.

**UnifAI**: Enterprise platform for building and running multi-agent AI workflows with visual UI, built-in RAG, and team collaboration.

Different products for different buyers. We sell to platform/security teams who need governed agent execution. They sell to AI teams who need a complete agent development platform.
