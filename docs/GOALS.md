# Cloud Agents Framework — Goals, Objectives & Technical Guidance

## Goals

### G1. Bring Your Own Agents & Workflows
Define agents and multi-step agentic workflows via YAML + any tools (Python, bash, CLI, MCP). No forking, no rebuilds, no framework changes. Product teams deploy AI agents without changing framework code.

### G2. Secured & Governed Execution
Each step runs in its own disposable container — scoped permissions, hard timeouts, no shared state. Untrusted pods never receive secrets beyond their API token. Human oversight on high-risk operations via approval gates. Full observability: tracing, metrics, event streaming.

### G3. Composable Agent Ecosystem
Agents and workflows are reusable building blocks. A chatbot invokes workflows as tools. Workflows chain agents. Multiple trigger points: conversations, alerts, API, schedules.

### G4. Seamless Human-Agent Handoff
When automation reaches its limit, users pick up in an AI CLI (Claude Code, Goose) with full workflow context — diagnosis, steps taken, failure history — and continue where the agent left off.

### G5. Dual Deployment: Kubernetes + Podman
Same agents run on both. Both are first-class production targets with behavioral parity but different security mechanisms.

## Design Principles

### R1. Framework, not pre-built agents [G1]
The diagnostic and monitoring agents are examples, not the product. The framework provides the runtime, executor, spawner, and observability. Product teams provide agent definitions, tool modules (Python, bash, CLI, MCP, etc.), workflow definitions, and optional skill packages. One generic container image for all agent types.

### R2. Multi-step workflows with human oversight [G1, G2]
Chain agents with conditions, retry, approval gates, escalation. Low-risk steps auto-approve; high-risk steps require human review.

### R3. Ephemeral-by-default [G2]
Fresh container per step. Only configured tools loaded. Hard timeouts. Destroyed after execution. Untrusted pods never receive secrets beyond their API token. Pre-deployed option for latency-sensitive steps.

### R4. Human-in-the-loop [G2]
Diagnose, Propose, Gate, Execute, Verify. Structured output: confidence, risk, rollback plan. Approval gates pause workflows for human review.

### R5. Human-out-of-the-loop [G2, G4]
When retries exhaust, escalation packages full context — diagnosis, steps taken, failure history, tools, cluster state. User picks up in an interactive AI CLI session and continues investigating where the agent left off.

### R6. Retry with escalation [G2, G4]
Each retry sees full failure history. Exhausted retries route to R4 (approval) or R5 (interactive handoff).

### R7. Stateless runner, durable state [G1, G5]
No in-memory state. Durable external store. Scales horizontally behind a load balancer. Replica crashes don't lose workflows.

## Technical Requirements

### R8. Execution engine [G1, G2]
Consider reuse/adapt OLS's sandbox for agent isolation, combined with Temporal workflow engine for durable orchestration.

### R9. Runtime [G1]
Pydantic AI + FastAPI + Temporal workflow (TBD).

### R10. Deployment [G5]
- **Kubernetes**: K8s Jobs for ephemeral agents, Services for DNS discovery, ConfigMaps for config distribution, Secrets for sensitive env vars, RBAC via ServiceAccounts.
- **Podman**: containers with volume mounts and port mapping, shared network for DNS, host-level access control.
- **One generic container image** (`agent-runtime:latest`) for all agent types.

### R11. Persistence [G1]
Durable store with optimistic locking. Atomic reads/writes, pluggable backend.

### R12. Security [G2]
Secrets via K8s refs, bearer auth on all endpoints. Explicit risk_level, per-step permission scoping.

### R13. Access control [G2]
RBAC for workflows: who can trigger, approve, view. RBAC for tools: which tools each agent/user can invoke. Scoped by team, role, or namespace.

### R14. Observability [G2]
OpenTelemetry distributed tracing, Prometheus metrics, SSE streaming for real-time progress events.

### R15. Triggers [G3]
Chatbot conversations, alert-based automation, API calls, scheduled events.

### R16. Agents-as-tools [G3]
Registry auto-generates tools from agent/workflow definitions.

### R17. Escalation [G4]
Context packaging: workflow state, failure history, tools, cluster context. Handoff to interactive AI CLI session.
