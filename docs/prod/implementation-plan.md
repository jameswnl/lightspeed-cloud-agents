# Productization — Implementation Plan

**Date**: 2026-06-29
**Source**: `productization-roadmap.md` (reviewed), `poc2/operator-comparison-code-review.md`
**Scope**: P0 tasks only + ARCHITECTURE.md rewrite. P1 items (per-workflow model provider, rate limiting, alerting, runbooks, circuit breaker, load testing, PDBs) deferred to a follow-up plan.
**Reviewed by**: `prod/implementation-plan-review.md` — 6 issues addressed (see below)

## Phases

### Phase 1: Operator parity (blocks multi-provider deployments)

#### T1: Credential Secret volume mount

Add Secret volume mount and `envFrom.secretRef` to `KubernetesSpawner` so file-based credential providers (Vertex `GOOGLE_APPLICATION_CREDENTIALS`, Bedrock) work.

**Files:**
- `src/agents/workflow/temporal_models.py` — `ProviderConfig.credentials_secret` already exists (used for env var injection). No schema change needed — the activity will additionally use this field to mount the Secret as a volume.
- `src/agents/spawner/kubernetes_spawner.py` — in `_do_spawn()`:
  - Mount Secret at `/var/run/secrets/llm-credentials/`
  - Add `envFrom` with `secretRef` for blanket key injection
- `src/agents/workflow/temporal_activities.py` — pass `credentials_secret` through to spawner

**Tests:**
- `tests/unit/agents/spawner/test_kubernetes_spawner.py` — verify Job spec includes Secret volume + envFrom
- `tests/unit/agents/workflow/test_temporal_activities.py` — verify credential secret name forwarded

**Verify:** `helm template` with credential secret configured produces correct Job YAML.

**Effort:** 1-2 days.

#### T2: MCP server injection

Pass `LIGHTSPEED_MCP_SERVERS` JSON env var to sandbox pods. Mount Secret-based MCP auth headers as volumes.

**Schema decision:** MCP servers are configured at **workflow level** on `WorkflowInput.mcp_servers`. All steps in the workflow see the same MCP config. Per-step override is not supported in this phase — if needed later, a step-level `mcp_servers` field would merge with (not replace) the workflow-level list.

**MCP server config shape** (matches upstream operator's convention):
```python
class MCPServerConfig(BaseModel):
    """One MCP server to inject into sandbox pods."""
    name: str                          # server identifier
    url: str                           # MCP server endpoint
    headers: dict[str, str] | None     # plain-text headers (non-sensitive)
    secret_headers: dict[str, SecretHeaderRef] | None  # headers from K8s Secrets

class SecretHeaderRef(BaseModel):
    """Reference to a K8s Secret key for an MCP auth header."""
    secret_name: str    # K8s Secret name (must be in same namespace)
    key: str            # key within the Secret
```

**Secret resolution contract:** The workflow runner (activity) never reads or inlines secret values. Secret-backed headers are encoded as **file references** in `LIGHTSPEED_MCP_SERVERS`. The sandbox pod resolves them locally from its own mounted Secret files. This preserves the file-based secret boundary — sensitive header values never appear in env vars or Temporal payloads.

**Worked example:**

YAML input:
```yaml
mcp_servers:
  - name: servicenow
    url: https://mcp.servicenow.internal/sse
    secret_headers:
      Authorization:
        secret_name: mcp-servicenow-token
        key: bearer-token
```

Resulting env var on sandbox pod (secret values are file refs, NOT inlined):
```json
LIGHTSPEED_MCP_SERVERS='[{"name":"servicenow","url":"https://mcp.servicenow.internal/sse","headers":{"Authorization":{"file":"/var/secrets/mcp/servicenow/bearer-token"}}}]'
```

Mounted Secret on sandbox pod:
```
/var/secrets/mcp/servicenow/bearer-token  ← from Secret mcp-servicenow-token
```

The sandbox reads the file at connect time to populate the header. This matches the operator's pattern where secrets are mounted as files and consumed by the sandbox process, not by the orchestrator.

**Upstream dependency:** The sandbox must support `{"file": "/path"}` header references in its MCP client. If not yet supported, this requires a small upstream contract extension (read file content as header value when `headers[key]` is an object with a `file` field instead of a string).

**Trust boundary:** See T2-security note below (Issue 3).

**Files:**
- `src/agents/workflow/temporal_models.py` — add `MCPServerConfig`, `SecretHeaderRef`, and `mcp_servers: list[MCPServerConfig] | None` to `WorkflowInput`
- `src/agents/workflow/temporal_activities.py` — in `_run_sandbox_step_inner()`:
  - Serialize `mcp_servers` as JSON with file refs (NOT resolved values) → set `LIGHTSPEED_MCP_SERVERS` env var
  - For entries with `secret_headers`, compute mount paths and collect secret mount specs → pass to spawner
- `src/agents/spawner/kubernetes_spawner.py` — accept `mcp_secret_mounts: list[tuple[str, str, str]]` (secret_name, key, mount_path) in `_do_spawn()`, add volume + volumeMount entries to the sandbox Job
- `src/agents/spawner/podman_spawner.py` — env var only (no Secret volume on Podman; secret_headers rejected with clear error)

**Tests:**
- Unit test: verify `LIGHTSPEED_MCP_SERVERS` env var set on spawned pod with correct JSON shape
- Unit test: verify Secret volumes mounted for MCP auth headers at `/var/secrets/mcp/{name}/`
- Unit test: Podman spawner with `secret_headers` → raises clear error
- Integration test: mock sandbox verifies it receives the MCP config

**Verify:** Spawn sandbox with MCP config → `env | grep MCP` inside container shows valid JSON.

**Effort:** 1 week.

**T2-security: MCP secret trust boundary**

Workflow authors can reference K8s Secrets in `secret_headers`. To prevent arbitrary secret exfiltration:

1. **Namespace scoping** — MCP Secret references must be in the same namespace as the workflow runner. The K8s API enforces this natively (Secrets are namespace-scoped), so cross-namespace references will fail at volume mount time.
2. **Allowlist (optional, off by default)** — env var `MCP_ALLOWED_SECRETS` accepts a comma-separated list of Secret names. When set, the activity validates that all referenced `secret_name` values appear in the allowlist before spawning. When unset, any Secret in the namespace is allowed (suitable for single-team deployments where workflow authors are trusted operators).
3. **Audit** — every MCP Secret mount emits an `AuditEvent` with `event_type="mcp_secret_mounted"`, `secret_name`, and `workflow_id`.

**Files:**
- `src/agents/workflow/temporal_activities.py` — validate secret references against allowlist before spawning
- `src/agents/workflow/audit.py` — emit audit event on secret mount

---

### Phase 2: Security hardening

#### T4: Pod security context

Add `securityContext` to Helm deployment template and spawned sandbox Jobs.

**Files:**
- `deploy/helm/cloud-agents-temporal/templates/deployment.yaml` — add container-level securityContext
- `deploy/helm/cloud-agents-temporal/values.yaml` — add `securityContext` values with defaults
- `deploy/kind/workflow-runner.yaml` — add securityContext
- `src/agents/spawner/kubernetes_spawner.py` — add `V1SecurityContext` to spawned Job containers:
  ```python
  security_context=client.V1SecurityContext(
      run_as_non_root=True,
      read_only_root_filesystem=True,
      allow_privilege_escalation=False,
  )
  ```
  Add `/tmp` tmpfs volume for write scratch space.

**Tests:**
- Unit test: verify Job spec includes securityContext
- `helm template` → verify securityContext in output YAML
- E2E: sandbox pod runs successfully with read-only root fs

**Effort:** 1 day.

#### T5: Structured logging + audit events

Switch to structured JSON logging. Add audit event model for security-relevant actions.

**Files:**
- `src/agents/workflow/temporal_entrypoint.py` — configure `structlog` in lifespan based on `LOG_FORMAT` env var
- `src/agents/workflow/audit.py` (new) — `AuditEvent` Pydantic model + `emit_audit()` helper:
  ```python
  class AuditEvent(BaseModel):
      event_type: str  # workflow_started, step_approved, sandbox_spawned, etc.
      workflow_id: str
      step_name: str | None
      actor: str | None  # who triggered / approved
      risk_level: str | None
      details: dict[str, Any]
      timestamp: str
  ```
- `src/agents/workflow/temporal_workflow.py` — emit audit events on workflow start, approval signal received, step complete
- `src/agents/workflow/temporal_activities.py` — emit audit events on sandbox spawn, sandbox destroy, escalation

**Tests:**
- Unit test: `LOG_FORMAT=json` → log output is valid JSON with trace_id, workflow_id
- Unit test: approval signal → `AuditEvent` with event_type="step_approved" emitted
- Unit test: `LOG_FORMAT=text` (default) → human-readable output preserved

**Effort:** 2-3 days.

#### T6: Graceful shutdown + crash recovery

Two contracts: clean SIGTERM drain for healthy shutdowns, and startup reconciliation for crashes.

**T6a: SIGTERM drain**

Wire Temporal Worker's built-in graceful shutdown to SIGTERM. The Temporal SDK's `async with Worker(...)` already calls `worker.shutdown()` on context exit, which drains in-flight activities. Verify this is wired correctly through the FastAPI lifespan and add a SIGTERM handler if the default uvicorn behavior doesn't propagate cleanly.

**Files:**
- `src/agents/workflow/temporal_entrypoint.py` — verify lifespan shutdown path; add explicit SIGTERM → lifespan exit if needed

**T6b: Crash-restart reconciliation**

On startup, scan for orphaned sandbox pods/containers left by a previous crash. The spawner labels every sandbox with `cloud-agents/workflow-id`, `cloud-agents/step-name`, `cloud-agents/attempt` — these labels are the durable source of truth.

**Files:**
- `src/agents/spawner/base.py` — add `async def list_active(self, labels: dict) -> list[str]` to `AgentSpawner` ABC
- `src/agents/spawner/kubernetes_spawner.py` — implement `list_active()`: list Jobs with `spawned-by=workflow-runner` label, return names
- `src/agents/spawner/podman_spawner.py` — implement `list_active()`: list containers with `cloud-agents/*` labels
- `src/agents/workflow/temporal_entrypoint.py` — on startup (in lifespan, before Worker starts):
  ```python
  orphans = await spawner.list_active({"spawned-by": "workflow-runner"})
  for name in orphans:
      logger.warning("Destroying orphaned sandbox '%s'", name)
      await spawner.destroy(name)
  ```
  Log count as an audit event.

**Tests:**
- Unit test (T6a): SIGTERM → worker exits cleanly, no orphaned containers
- Unit test (T6b): pre-existing labeled containers → destroyed on startup
- Integration test: kill worker mid-activity → restart → orphans cleaned up

**Effort:** 2 days.

---

### Phase 3: CI + upstream

#### T7: Image registry push + signing

Add push and signing to the image build workflow.

**Files:**
- `.github/workflows/build_workflow_runner.yaml` — add steps:
  - `buildah push` to `quay.io/redhat/workflow-runner:$TAG` on main merge
  - `cosign sign` with OIDC keyless signing
  - Tag strategy: `latest` + git SHA + semver tag if present

**Tests:**
- CI: workflow runs on main merge → image appears in registry
- `cosign verify` succeeds on pushed image

**Effort:** 1 day.

#### T8: Upstream sandbox PRs

Submit PRs to `lightspeed-agentic-sandbox` upstream (fork branch `temporal-integration` + new MCP work).

**PRs:**
1. `executionResult` context formatting in `_format_context_prefix()` (~15 lines) — code complete on fork
2. HTTP 502 for `ConnectionError`, `TimeoutError`, `RateLimitError` (~25 lines + `_is_infrastructure_error()`) — code complete on fork
3. **MCP server support with file-reference headers** (new, required by T2) — see T8a below

**Prerequisite:** Discussion with upstream maintainers (already noted as pending).

**Effort:** PRs 1-2: 1-2 days. PR 3: see T8a.

#### T8a: Sandbox MCP file-reference header support

**Context:** T2 specifies that `LIGHTSPEED_MCP_SERVERS` encodes secret-backed headers as file references (`{"file": "/var/secrets/mcp/..."}`) rather than inlined values. The sandbox must resolve these at MCP connect time. The sandbox currently has **no MCP support at all** — `LIGHTSPEED_MCP_SERVERS` is not read anywhere.

**T2 depends on T8a.** Without this, MCP server injection has no sandbox-side consumer.

**What to build** (in `lightspeed-agentic-sandbox`):
1. Read `LIGHTSPEED_MCP_SERVERS` env var (JSON list of server configs)
2. For each server, register it as an MCP tool source on the agent
3. When a header value is `{"file": "/path"}` instead of a plain string, read the file content and use it as the header value at connect time
4. Wire MCP tools into the OpenAI agents SDK tool registry

**Files** (in sandbox repo):
- `src/lightspeed_agentic/mcp.py` (new) — MCP config parser, file-ref resolver, server registration
- `src/lightspeed_agentic/providers/openai.py` — integrate MCP tools alongside Shell/Filesystem/Skills

**Tests:**
- Unit test: parse `LIGHTSPEED_MCP_SERVERS` JSON with file-ref headers
- Unit test: file-ref resolver reads mounted Secret file, returns content as header value
- Integration test: sandbox connects to mock MCP server using resolved file-ref auth header

**Verification** (the critical edge from the review):
- Sandbox receives `{"file": "/var/secrets/mcp/servicenow/bearer-token"}`
- Sandbox reads the mounted file
- Outbound MCP connection uses the resolved header value

**Effort:** 3-5 days (new capability in sandbox).

---

### Phase 4: Documentation

#### T9: ARCHITECTURE.md rewrite

Full rewrite to reflect the Temporal + sandbox architecture.

**Replace:**
- `WorkflowExecutor` / `StepDispatcher` / `RecoveryPoller` → Temporal AgentWorkflow + activities
- `pydantic-ai Agent` in pods → OpenAI agents SDK in lightspeed-agentic-sandbox
- `agent-runtime:latest` with ConfigMap mounts → sandbox with env var contract
- PostgreSQL persistence → Temporal Server durable execution
- ASCII diagram → updated diagram matching HTML visualization

**Add:**
- PoC2 phases to phase history table
- Temporal Server as a component
- POST /v1/agent/run contract summary
- Env var contract (LIGHTSPEED_PROVIDER, LIGHTSPEED_MODEL, credentials)

**File:** `docs/design/cloud-agents/ARCHITECTURE.md`

**Effort:** Half day.

#### T10: API idempotency documentation

Document that `workflow_id` serves as Temporal's idempotency key. Handle `WorkflowAlreadyStartedError` → return 409 Conflict.

**Files:**
- `src/agents/workflow/temporal_api.py` — catch `WorkflowAlreadyStartedError` in `/run` endpoint, return 409
- `docs/design/cloud-agents/sandbox-contract.md` — add API idempotency section

**Effort:** Half day.

---

## Dependencies

```
T1 (credentials)    ── independent
T8a (sandbox MCP)   ── must complete before T2 (sandbox has no MCP support yet)
T2 (MCP injection)  ── depends on T8a

T4 (pod security) ─┐
T5 (audit logging) ┤── independent, can parallel
T6 (shutdown)      ─┘

T7 (CI push)       ── independent
T8 (upstream PRs)   ── blocked on maintainer discussion; T8a is part of T8

T9 (arch docs)     ─┐
T10 (idempotency)  ─┘── independent, can parallel
```

## Execution Order

| Week | Tasks | Focus |
|------|-------|-------|
| 1 | T1, T8a | Operator parity: credentials + sandbox MCP support (parallel) |
| 2 | T2, T4, T5, T6 | MCP injection (unblocked by T8a) + security hardening (parallel) |
| 3 | T7, T8 | CI + upstream PRs (including T8a) |
| 4 | T9, T10 | Documentation |

All tasks within each week are independent and can be parallelized.

## Verification Checklist

- [ ] Vertex/Bedrock workflow succeeds (credential Secret mounted at `/var/run/secrets/llm-credentials/`)
- [ ] MCP-enabled workflow calls external tool server via `LIGHTSPEED_MCP_SERVERS`
- [ ] MCP Secret mount emits audit event; allowlist blocks unauthorized secrets when configured
- [ ] `kubectl describe pod` shows securityContext on workflow-runner and sandbox pods
- [ ] `LOG_FORMAT=json` produces structured JSON logs with trace_id, workflow_id
- [ ] Approval signal → audit event in logs with actor, risk_level, event_type
- [ ] SIGTERM → worker drains gracefully, no orphaned sandbox containers
- [ ] Restart after crash → orphaned sandbox pods/containers cleaned up via label scan
- [ ] Image pushed to quay.io, `cosign verify` succeeds
- [ ] Upstream sandbox PRs merged (or submitted + in review)
- [ ] ARCHITECTURE.md describes Temporal + sandbox architecture
- [ ] Duplicate `workflow_id` submission → HTTP 409

## Test Commands

```bash
# Unit + integration (no infra)
uv run pytest tests/unit/agents/ tests/integration/temporal/ -q

# Structured logging verification
LOG_FORMAT=json uv run python -c "from agents.workflow.temporal_entrypoint import app; print('ok')"

# Helm template validation
helm template deploy/helm/cloud-agents-temporal/ | grep -A5 securityContext

# E2E with Temporal
uv run pytest tests/e2e/temporal/ -v
```
