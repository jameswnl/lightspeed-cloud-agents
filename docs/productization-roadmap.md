# Cloud Agents — Productization Roadmap

**Date**: 2026-06-29
**Status**: PoC2 Phases 1-3 complete. System works end-to-end. Operator parity gaps (credential mounts, MCP) identified post-Phase 3 and not yet implemented.
**Inputs**: Codebase survey, `poc2/operator-comparison-code-review.md` (gap analysis vs lightspeed-agentic-operator), `BACKLOG.md`
**Reviewed by**: `poc2/productization-roadmap-review.md` (2026-06-29)

## Current State

The cloud agents framework runs end-to-end: Temporal workflow engine → Spawner → lightspeed-agentic-sandbox container → LLM → structured output. Verified with a real diagnostic workflow against a Kind cluster.

### What's implemented

| Area | Component | Status |
|------|-----------|--------|
| **Orchestration** | Temporal AgentWorkflow with conditions, interpolation, parallel groups, signals | Done |
| **Policy** | Auto-approval by risk level, advisory mode (read-only SA + filesystem), PermissionScope | Done |
| **Spawners** | KubernetesSpawner (K8s Jobs), PodmanSpawner (Podman containers), skills image/volume support | Done |
| **Sandbox** | lightspeed-agentic-sandbox with OpenAI agents SDK, POST /v1/agent/run contract, structured output | Done |
| **Auth** | Bearer token + K8s TokenReview, fail-closed AUTH_REQUIRED, exempt health paths | Done |
| **TLS** | TEMPORAL_TLS_ENABLED with cert/key/CA paths for Temporal gRPC | Done |
| **Tracing** | OTel init, spans on all activities, TracingInterceptor on Temporal Worker | Done |
| **Metrics** | ls_workflow_runs_total, ls_workflow_step_runs_total, duration histograms, /metrics endpoint | Done |
| **Health** | /healthz, /livez (stale detection), /readyz (Temporal connectivity) | Done |
| **Containerfile** | python:3.12-slim, multi-stage, non-root UID 1001 | Done |
| **Helm** | Chart with deployment, service, RBAC, NetworkPolicy templates | Done |
| **CI** | build_workflow_runner.yaml (Buildah), e2e_tests_temporal.yaml (Temporal service container) | Done |
| **Network** | Ingress + egress policies, sandbox egress for LLM CIDRs (opt-in via Helm) | Done |
| **Contract docs** | sandbox-contract.md (request/response schema, env vars, error classification) | Done |
| **Validation** | `temporal_validation.py` — duplicate steps, undefined refs, missing output_keys at submission time | Done |
| **Tests** | 46 unit, 2 integration (policy, sandbox contract), 2 E2E (Temporal server, container build) | Done |

### Key files

- `src/agents/workflow/temporal_workflow.py` — AgentWorkflow
- `src/agents/workflow/temporal_activities.py` — sandbox step execution
- `src/agents/workflow/temporal_api.py` — REST API (/run, /approve, /{id}, /definitions)
- `src/agents/workflow/temporal_entrypoint.py` — FastAPI app with lifespan, probes, metrics
- `src/agents/spawner/kubernetes_spawner.py`, `podman_spawner.py` — dual-target spawners
- `deploy/workflow-runner/Containerfile` — container image
- `deploy/helm/cloud-agents-temporal/` — Helm chart

---

## P0 — Must-have for production

### Structured logging + audit events

**Gap**: Standard Python `logging` with plain text format. No compliance trail for approval decisions, tool executions, or policy overrides. Centralized log aggregation (Loki, ELK, Splunk) requires structured JSON; SOC2/FedRAMP requires audit trails.

**Where**: Every module uses `logging.getLogger(__name__)` with default formatter. Approval signals in `temporal_workflow.py`, risk classification in `auto_approve.py`, activity execution in `temporal_activities.py` — all log informally.

**What to build** (single task, two layers):
1. **JSON logging**: Configure `structlog` or `python-json-logger` in the entrypoint. Include trace_id, span_id, workflow_id, step_name as structured fields. Env-var toggle (`LOG_FORMAT=json`) to keep human-readable output in dev.
2. **Audit events**: Define an `AuditEvent` Pydantic model for security-relevant actions (workflow started, step approved/rejected with risk level, sandbox spawned/destroyed, escalation triggered). Emit via the same structured logger. Same JSON pipeline, same log stream, different event schemas.

### Pod security context

**Gap**: Containerfile runs non-root (UID 1001) but K8s manifests don't enforce `securityContext`. A compromised pod could escalate privileges.

**Where**: `deploy/helm/cloud-agents-temporal/templates/deployment.yaml` and `deploy/kind/workflow-runner.yaml` — no `securityContext` block. `kubernetes_spawner.py` creates Jobs without security context on spawned pods.

**What to build**: Add to Helm deployment template:
```yaml
securityContext:
  runAsNonRoot: true
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  seccompProfile:
    type: RuntimeDefault
```
Apply same to spawned sandbox Jobs in `kubernetes_spawner.py`. Add `tmpfs` mount for `/tmp` since readOnlyRootFilesystem blocks writes.

### Graceful shutdown

**Gap**: FastAPI lifespan context manager exists but no SIGTERM handler to drain in-flight Temporal activities. Abrupt shutdown can orphan sandbox containers.

**Where**: `temporal_entrypoint.py` creates a Temporal Worker in `build_temporal_app()` lifespan. No signal handler registered.

**What to build**: Register SIGTERM handler that sets a shutdown flag, stops accepting new workflows, waits for in-flight activities to complete (with timeout), then calls `spawner.destroy()` on any remaining pods. The Temporal Worker has built-in graceful shutdown (`worker.shutdown()`) — wire it to the signal.

### Image registry push + signing

**Gap**: `build_workflow_runner.yaml` builds the image but doesn't push to a registry or sign it. No supply chain verification.

**Where**: `.github/workflows/build_workflow_runner.yaml` — uses `buildah-build` action, no `buildah-push`.

**What to build**: Add push step for `quay.io/redhat/workflow-runner` on main merge. Add cosign image signing. Consider adding SBOM generation (syft) as a follow-up.

### Credential volume mount (blocks Vertex/Bedrock)

**Gap**: The operator mounts the credential Secret as a volume at `/var/run/secrets/llm-credentials/` and injects all keys via `envFrom.secretRef`. Cloud Agents only passes individual env vars via `SecretKeyRef`. File-based credential providers (Vertex `GOOGLE_APPLICATION_CREDENTIALS`, Bedrock) fail because the file path doesn't exist.

**Where**: `kubernetes_spawner.py` — only supports per-key `SecretKeyRef` env injection. No Secret volume mount.

**What to build**: In `KubernetesSpawner._do_spawn()`, add:
1. A Secret volume mount at `/var/run/secrets/llm-credentials/` using the existing `ProviderConfig.credentials_secret` field (no schema change needed)
2. `envFrom.secretRef` to inject all Secret keys as env vars (covers providers that read env vars directly)

**Source**: `poc2/operator-comparison-code-review.md` Gap 1. Effort: 1-2 days.

### MCP server injection (blocks extensibility)

**Gap**: The operator sets `LIGHTSPEED_MCP_SERVERS` as a JSON env var on sandbox pods. For MCP headers referencing K8s Secrets, it mounts those Secrets at `/var/secrets/mcp/{server-name}/`. Cloud Agents does not pass MCP config to sandbox pods at all.

**Where**: `temporal_activities.py` `run_sandbox_step` — builds env vars but has no MCP handling. `temporal_models.py` `WorkflowInput` — no `mcp_servers` field.

**What to build**:
1. Add `mcp_servers` field to step config / `WorkflowInput`
2. In the activity, serialize MCP server configs as JSON and set `LIGHTSPEED_MCP_SERVERS` env var
3. For MCP servers with Secret-based auth headers, mount those Secrets as volumes

**Why it matters**: Without MCP, product teams can only use the sandbox's built-in tools (Shell, Filesystem, Skills). MCP enables calling external tool servers (ServiceNow, PagerDuty, Jira) without changing the sandbox image.

**Source**: `poc2/operator-comparison-code-review.md` Gap 2. Effort: 1 week.

### Upstream sandbox PRs (executionResult + HTTP 502)

**Gap**: Two changes to `lightspeed-agentic-sandbox` are implemented on fork branch `temporal-integration` but not yet submitted upstream:
1. **executionResult context formatting** — `_format_context_prefix()` includes execution results from prior steps. ~15 lines in sandbox's `query.py`.
2. **HTTP 502 for infrastructure errors** — returns HTTP 502 for `ConnectionError`, `TimeoutError`, `RateLimitError` so the activity can classify retry-vs-fail. ~25 lines with `_is_infrastructure_error()` classifier.

**Status**: Code complete on fork (`jameswnl/lightspeed-agentic-sandbox` branch `temporal-integration`). Not submitted upstream — needs discussion with upstream maintainers first.

**Why P0**: Without HTTP 502, the activity falls back to string-matching heuristics for error classification. Without executionResult, multi-step workflows lose context between steps.

---

## P1 — Should-have before GA

### Prometheus alerting rules

**Gap**: Metrics exported but no alert definitions. Nobody gets paged when workflows fail or pods leak.

**What to build**: PrometheusRule CRD or alerting rules file. Key alerts:
- `ls_workflow_step_runs_total{status="failed"}` rate > threshold
- Sandbox container lifetime > timeout (orphaned pod)
- Temporal Worker heartbeat missing (worker down)
- LLM provider error rate spike

### Operational runbooks

**Gap**: No troubleshooting documentation for common failure modes.

**What to build**: `docs/operations/cloud-agents-runbook.md` covering:
- Temporal Server unreachable (symptoms, diagnosis, recovery)
- Sandbox pod stuck in Pending (resource pressure, image pull failures)
- LLM provider timeout cascade
- Orphaned containers (how to find, how to clean up)
- How to manually approve/reject a waiting workflow

### Circuit breaker for LLM provider

**Gap**: If the LLM provider is down, sandbox pods keep spawning and timing out — wasting resources.

**Where**: `temporal_activities.py` `run_sandbox_step` spawns → calls → destroys regardless of provider health.

**What to build**: Track recent failures per provider. After N consecutive failures in M seconds, short-circuit sandbox spawning and fail fast. Reset on success. Could use Temporal's built-in activity retry policy with `non_retryable_error_types` for 502 responses.

### Load and stress testing

**Gap**: No tests for concurrent workflow spawning, Temporal worker saturation, or pod resource exhaustion.

**What to build**: `tests/load/` with scenarios: 10 concurrent workflows, 50-step workflow, rapid spawn/destroy cycling. Use `locust` or simple async script against the API.

### Template interpolation sanitization

**Gap**: `interpolation.py` substitutes user-provided values into prompts via `{{ steps.X.output.Y }}`. No escaping or validation of interpolated content.

**Where**: `src/agents/workflow/interpolation.py` — string template substitution.

**What to build**: Validate that interpolated values don't contain template syntax (preventing recursive interpolation). Length-limit interpolated values. Log a warning for unexpectedly large substitutions.

### Per-workflow model provider derivation

**Gap**: `LIGHTSPEED_MODEL_PROVIDER` is forwarded from the worker's process env to all sandbox pods. Can't vary per workflow (e.g., one workflow uses Claude on Vertex, another uses Gemini on Vertex).

**Where**: `temporal_activities.py` forwards `LIGHTSPEED_MODEL_PROVIDER` statically from `os.environ`.

**What to build**: Add `model_provider` field to `ProviderConfig`. The activity sets `LIGHTSPEED_MODEL_PROVIDER` from this field. Default to env var fallback for backward compat.

**Source**: `poc2/operator-comparison-code-review.md` Gap 3. Effort: 1 day.

### Rate limiting

**Gap**: No per-user or per-agent request-level rate limiting. The spawner has `MAX_SPAWNED_PODS=10` and the Temporal worker has `max_concurrent_activities` which prevent pod storms, but there's no request-level throttling.

**Why P1 not P0**: Existing spawner + worker concurrency caps are sufficient for single-team deployments. Per-user rate limiting becomes critical for multi-tenant shared deployments.

**What to build**: FastAPI middleware with configurable limits per caller identity. Consider `slowapi` or a simple token-bucket. The spawner caps are the safety net; rate limiting is the user-facing policy.

### Pod disruption budgets

**Gap**: Helm chart has no PDB. Rolling upgrades could kill all workflow runner replicas simultaneously.

**What to build**: Add PDB template to Helm chart: `minAvailable: 1` when replicas > 1.

---

## Backlog (post-GA)

See `BACKLOG.md` for the full itemized list. Key additions beyond what's already tracked:

| Item | Source |
|------|--------|
| Dynamic RBAC from agent output (per-proposal SA + Role/RoleBinding) | Operator comparison Gap 4 |
| Native K8s image volumes for skills (K8s 1.31+, fallback to init container) | Operator comparison Gap 6 |
| Template reuse / content-hash dedup for pod specs | Operator comparison Gap 7 |
| Per-job identity binding (scoped SA per sandbox pod) | BACKLOG.md |
| Tool origin validation allowlist | BACKLOG.md |
| Agents/workflows as LLM tools | BACKLOG.md |
| Conversational approval in chat UI | BACKLOG.md |
| Nested workflows | BACKLOG.md |
| SBOM / SLSA provenance for images | New |
| Multi-replica failover E2E testing | BACKLOG.md |
| Capacity planning guide (concurrent pods, DB sizing) | New |
| SSE resume tokens for event reconnection | BACKLOG.md |
| Workflow visualization (UI or console plugin) | BACKLOG.md |
| CRD-based K8s operator for GitOps | BACKLOG.md |

---

## Documentation Debt

### ARCHITECTURE.md rewrite needed

`docs/design/cloud-agents/ARCHITECTURE.md` still describes the **pre-Temporal architecture**:
- References `WorkflowExecutor`, `StepDispatcher`, `RecoveryPoller` (deleted in PoC2)
- Shows PostgreSQL as persistence layer (now Temporal Server)
- ASCII diagram is completely outdated
- **Partially resolved**: `pydantic-ai Agent` and `agent-runtime:latest` references have been removed; Agent Definition section deleted

Needs a full rewrite to reflect:
- Temporal AgentWorkflow + activities
- lightspeed-agentic-sandbox with POST /v1/agent/run contract
- Env var contract (LIGHTSPEED_PROVIDER, LIGHTSPEED_MODEL, credentials)
- Temporal Server as durable execution + state layer
- Updated ASCII diagram matching the HTML visualization

### Phase history table

Phase table in ARCHITECTURE.md stops at Phase 7. Needs PoC2 phases added.

### API idempotency behavior

Temporal already handles idempotency via `workflow_id` in `WorkflowInput` — duplicate submissions get `WorkflowAlreadyStartedError`. Document this behavior in API docs. Consider returning 409 Conflict instead of 500 for duplicate `workflow_id` submissions in `temporal_api.py`.
