# Cloud Agents Framework — Backlog

Unscheduled items deferred from completed phases. Pick based on priority and team capacity.

## Security

- **K8s per-pod identity via TokenReview API** — projected SA tokens are pod-specific, can't be compared cross-pod. Needs cluster-side validation on the callee. *(deferred from Phase 7 Task 3)*
- **Tool origin validation allowlist** — `load_tools()` uses `importlib.import_module()` with module paths from YAML. Add optional `allowed_tool_modules` in runner config. *(deferred from Phase 7 Task 3b)*
- **Approval routing with channel plugins + RBAC** — per-step approver scoping, Slack/webhook/conversational channels, audit trail. *(design doc: approval-routing-design.md)*

## K8s Robustness

- **AlreadyExists idempotent Job creation** — KubernetesSpawner should handle `AlreadyExists` on Job creation for safe retry. *(deferred from Phase 7 Task 4)*
- **Workflow visibility labels on spawned Jobs** — add `workflow-id`, `step-name`, `created-at` labels for operational debugging. *(deferred from Phase 7 Task 5)*
- **CRD-based K8s operator** — thin CRD-to-executor bridge for kubectl/GitOps workflows. *(from kubeclaw comparison)*

## Core Engine

- **pydantic-ai replaces Llama Stack** — core conversation engine swap. All new code already uses pydantic-ai.
- **Agents/workflows as tools** — registered agents and workflows become pydantic-ai tools the LLM calls autonomously during /query conversations.
- **Conversational approval** — when a workflow hits an approval gate, the LLM surfaces it to the user in natural language; user approves/rejects in the conversation flow.
- **Async callback dispatch** — ephemeral pods POST results to trusted runner ingest API instead of synchronous RemoteAgentClient.run(). *(partially designed in Phase 6, not implemented)*

## Workflow Features

- **Nested workflows** — workflow-to-workflow composition (recursive executor).
- **Workflow versioning and rollback** — schema migration + state compatibility.
- **Resumable SSE streaming** — persisted event replay via `Last-Event-ID`. *(deferred from Phase 6)*

## Infrastructure

- **Agent artifact storage** — OCI artifacts, derived images, git-sync sidecar for tool/skill distribution. *(design doc: artifact-storage-design.md)*
- **Workflow visualization** — graph rendering UI or OpenShift console plugin integration.
- **Multi-replica E2E with PostgreSQL** — 2-replica Kind deployment with real PostgreSQL, replica failover test. *(deferred from Phase 6 Task 7)*
