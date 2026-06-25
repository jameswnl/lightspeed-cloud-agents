# Cloud Agents Framework — Goals, Objectives & Technical Guidance

## What This Is

An agent/workflow orchestration platform built into lightspeed-stack. It enables product teams to create, deploy, and manage AI agents and multi-step workflows as server-side services in customer clusters (OCP/K8s or Podman).

## Goals

### G1. Bring your own agents
Product teams define agents via YAML and bring their own tools — Python functions, bash scripts, CLI wrappers, MCP servers, or any callable the runtime can invoke. The framework runs them. No forking, no image rebuilds, no PRs to the platform repo. One generic container image serves all agent types — agent identity comes entirely from mounted configuration.

### G2. Multi-step workflows with human oversight
Chain agents into workflows with conditions, retry, approval gates, and escalation. Humans stay in the loop for high-risk operations. Policy-driven approval classifies steps by risk level. Low-risk steps can auto-approve; high-risk steps require human review.

### G3. Ephemeral isolated execution
Each workflow step runs in its own disposable container. Clean state, scoped permissions, hard timeouts. A stuck or misbehaving agent can't affect other steps or the platform. Pre-deployed (long-running) agents are supported for cases where startup latency matters more than isolation.

### G4. Dual deployment targets
The same agents and workflows run on both Kubernetes (OCP) and Podman. Product teams like Ansible and RH Developer Hub ship GA features on Podman. Both are first-class production targets with behavioral parity but different security mechanisms.

### G5. Stateless horizontal scaling
The workflow runner is stateless and scales horizontally behind a load balancer. All state lives in a durable external store. Any replica can serve any request. Replica crashes don't lose workflows. Concurrent replicas are safe via optimistic locking — no duplicate advancement, no lost updates.

### G6. Production-grade observability
OpenTelemetry distributed tracing, per-tool Prometheus metrics, SSE streaming for real-time progress, structured logging with correlation IDs.

### G7. Production-grade security
Ephemeral pods are untrusted — they never receive database credentials or secrets beyond their scoped API token. Step results flow through the trusted runner, not directly to the database. Tool filtering enforces read-only access when needed. Auth middleware on all endpoints.

## Design Principles

### Framework, not pre-built agents
The diagnostic and monitoring agents are examples, not the product. The framework provides the runtime, executor, spawner, persistence, and observability. Product teams provide agent definitions, tool modules (Python, bash, CLI, MCP, etc.), workflow definitions, and optional skill packages.

### Ephemeral-by-default
Every workflow step spawns a fresh container that starts clean, has only its configured tools, has hard timeouts, has scoped permissions, and is destroyed after execution.

### Stateless runner, durable state
The workflow runner holds no state in memory. All workflow state, step results, and definitions live in a durable external store. This enables horizontal scaling, pod resilience, cross-replica operations, and crash recovery.

### Human-in-the-loop by design
The recommended workflow pattern follows phased execution: Diagnose, Propose, Gate, Execute, Verify. Every agent returns structured output with confidence, risk level, rollback plan, and required permissions — making responses reviewable, comparable, and actionable.

### Retry with escalation
Failed steps retry with full failure history. Each attempt sees what was tried before and why it failed. After exhausting retries, the framework generates an escalation handoff document for human operators with all evidence collected.

## Technical Guidance

### Runtime
- **Pydantic AI** is the agent framework. All new code uses pydantic-ai.
- **FastAPI** for all HTTP services (agent runtime, workflow runner).
- **Python** — check `pyproject.toml` for supported versions.

### Persistence
- Durable external store for workflow state, step results, and definitions.
- Must support **optimistic locking** (compare-and-swap) for multi-replica safety.
- Must support atomic reads and writes to prevent partial state corruption.
- Pluggable backend — in-memory and file-based backends for dev/testing.

### Deployment
- **Kubernetes**: K8s Jobs for ephemeral agents, Services for DNS discovery, ConfigMaps for config distribution, Secrets for sensitive env vars, RBAC via ServiceAccounts.
- **Podman**: containers with volume mounts and port mapping, shared network for DNS, host-level access control.
- **One generic container image** (`agent-runtime:latest`) for all agent types. Agent identity from mounted YAML + tools.

### Security
- API keys via K8s Secrets (`secretKeyRef`), never plain env vars in pod specs.
- Bearer auth on all cross-pod HTTP calls.
- Explicit risk_level on workflow steps — missing risk_level fails closed to "high."
- Per-step permission scoping (`allowed_tools` / `denied_tools`).
- Ephemeral pods never get database credentials.

### Observability
- OpenTelemetry distributed tracing across runner, agent pods, and LLM calls.
- Per-tool Prometheus metrics (`ls_agent_tool_calls_total`, `ls_agent_tool_duration_seconds`).
- SSE streaming for real-time workflow progress events.
- Correlation IDs validated and propagated across all requests.

### Testing
- **TDD is mandatory** — write failing tests before implementation.
- **pytest** for unit and integration tests (not unittest).
- **behave** for E2E tests with Gherkin feature files.
- Both unit tests and E2E tests required for deployment-touching features.
- E2E tests on both Kind (K8s) and Podman.

### Code Standards
- Follow patterns in CLAUDE.md: absolute imports, Google docstrings, type annotations, `logger = get_logger(__name__)`.
- Use `uv run make format` and `uv run make verify` before completion.
- No in-place parameter modification — return new data structures.
