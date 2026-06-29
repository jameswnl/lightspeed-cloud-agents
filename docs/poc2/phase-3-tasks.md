# PoC2 Phase 3: Operational Hardening + Sandbox Wiring

## Context

Phases 1-2 delivered the Temporal workflow engine with policy layer, LGTM'd after multiple review rounds. The sandbox execution path runs in stub mode — no real sandbox pod has been spawned. This phase:

1. **Wires the real sandbox execution path** (T0) — the core deliverable
2. **Hardens for single-team deployment** — container images, observability, Helm chart, TLS, network policies
3. **Adds CI** — image build + E2E pipelines

**Scope boundary**: This phase targets single-team deployments where all authenticated users are trusted operators. Multi-team RBAC (per-workflow authorization, approval-role scoping, audit logging) is explicitly deferred to Phase 4. This phase does NOT claim full production readiness for multi-tenant environments.

**Supersession note**: Phase 2 was described as "the final phase" — that referred to the PoC's feature scope. Phase 3 addresses the operational gaps discovered during review: the stub sandbox path, missing container images, and lack of observability. Phase 4 (future) will address authorization and multi-tenancy.

**Deferred to Phase 4** (intentionally out of scope):
- Per-workflow RBAC (who can trigger/approve/view)
- Approval audit logging
- Multi-tenant namespace isolation
- Request-level tool filtering (sandbox contract gap)

## Contracts (from plan review round 1)

### Sandbox Env Var Contract

The sandbox's `config.py:resolve_sdk()` reads these env vars. Source of truth for each:

| Env Var | Source | Required |
|---------|--------|----------|
| `LIGHTSPEED_PROVIDER` | `ProviderConfig.name` | Yes |
| `LIGHTSPEED_MODEL` | `ProviderConfig.model` | Yes |
| `LIGHTSPEED_MODEL_PROVIDER` | **Deployment config** (env var on workflow-runner) | Provider-dependent |
| `LIGHTSPEED_PROVIDER_URL` | **Deployment config** (env var on workflow-runner) | Provider-dependent |
| `LIGHTSPEED_PROVIDER_PROJECT` | **Deployment config** (env var on workflow-runner) | Gemini only |
| `LIGHTSPEED_PROVIDER_REGION` | **Deployment config** (env var on workflow-runner) | Gemini only |
| `LIGHTSPEED_PROVIDER_API_VERSION` | **Deployment config** (env var on workflow-runner) | Optional |

**Schema rule**: `ProviderConfig` stays as `{name, model, credentials_secret}` — it defines WHAT to use. The 5 deployment-dependent vars (`MODEL_PROVIDER`, `PROVIDER_URL`, `PROJECT`, `REGION`, `API_VERSION`) come from the workflow-runner's own environment and are propagated to spawned sandbox pods as-is. This keeps workflow definitions portable across environments (the same workflow YAML works in dev and prod — only the runner's env changes).

Credential env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) are resolved from `credentials_secret` via K8s `SecretKeyRef` or Podman host env propagation.

**T0 implementation**: The activity reads deployment vars from `os.environ` and passes them to spawned pods alongside `ProviderConfig` values. No schema migration needed — `ProviderConfig` is unchanged.

### Helm Chart Scope

**Option A (chosen)**: Workflow-runner + worker only. Assumes Temporal Server deployed separately (managed Temporal, existing deployment, or separate Helm release). This keeps the chart focused and avoids coupling to Temporal Server lifecycle.

The chart includes: workflow-runner Deployment + Service, RBAC, NetworkPolicies, ConfigMap for defaults. Temporal Server connection via `values.yaml` settings.

### Workflow Cancellation

`POST /v1/workflows/{id}/cancel` already exists in `temporal_api.py`. No new task needed — already implemented in Phase 1.

## Tasks

### P0 — Blocking

#### T0: Wire spawner to real sandbox contract
Replace stub activity path with real spawner calls. This is the core deliverable.

**Runtime contract:**
1. **Spawn**: Use `spawner.spawn()` with `lightspeed-agentic-sandbox` image (not generic agent-runtime). Set `LIGHTSPEED_PROVIDER` and `LIGHTSPEED_MODEL` from `ProviderConfig`. Forward `LIGHTSPEED_MODEL_PROVIDER`, `LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, `LIGHTSPEED_PROVIDER_REGION`, `LIGHTSPEED_PROVIDER_API_VERSION` from the workflow-runner's own `os.environ` (deployment config). Set credential env var from `credentials_secret` via K8s `SecretKeyRef` or Podman host env propagation.
2. **Wait**: `spawner.wait_ready(endpoint)` polls the sandbox health endpoint. The sandbox exposes `/health` (not `/healthz`). Update `SpawnConfig.health_path` default or pass `health_path="/health"` to `wait_ready()` for sandbox steps.
3. **Call**: Authenticated `POST {endpoint}/v1/agent/run` with request body matching documented contract (query, context, systemPrompt, outputSchema).
4. **Parse**: Handle sandbox response: `success=true` → completed, `success=false` → failed, HTTP 502 → raise for Temporal retry.
5. **Destroy**: `spawner.destroy(pod_name)` in `finally` block. On crash, label-based cleanup (`cloud-agents/workflow-id`).
6. **Idempotency**: Content-hash pod naming ensures retry spawns the same pod name.

**Tests required:**
- Non-stub E2E: deploy sandbox image to Kind cluster, run a real workflow that spawns a sandbox pod, calls the LLM, and returns a result
- Verify pod creation and cleanup via `kubectl get pods`
- Verify retry on HTTP 502 (simulate with a failing sandbox)

**Depends on:** T8 (contract documented first), T1 (Containerfile for workflow-runner).

#### T1: Workflow-runner Containerfile
Create `deploy/workflow-runner/Containerfile`. Python 3.12-slim, uv sync, entrypoint `uvicorn agents.workflow.temporal_entrypoint:app`.

#### T2: OTel tracing init in entrypoint
Call `init_tracing("workflow-runner")` in `temporal_entrypoint.py` lifespan.

#### T3: Trace spans in activities
Wrap activities with OTel spans. Set span attributes (step_name, workflow_id, pod_name).
**Depends on:** T2.

#### T4: Prometheus metrics module
Create `src/agents/workflow/temporal_metrics.py` with workflow/step counters and histograms.

#### T5: /metrics endpoint
Add `/metrics` to workflow-runner FastAPI app.
**Depends on:** T4.

### P1 — Required for production

#### T6: Helm chart (workflow-runner only)
Create `deploy/helm/cloud-agents-temporal/` with templates for workflow-runner Deployment + Service, RBAC, NetworkPolicies, ConfigMap. `values.yaml` exposes image, Temporal URL, resources, auth. Does NOT include Temporal Server (deployed separately).
**Depends on:** T1.

#### T7: Egress network policies
Workflow-runner → Temporal (7233) + sandbox (8080) + K8s API (443). Sandbox → LLM + DNS. Block rest. Opt-in via Helm values.

#### T8: Sandbox contract documentation
Document POST `/v1/agent/run` request/response schema, full env var contract (7 vars), error classification (200 vs 502), volume mounts.

#### T9: CI image build workflow
Build with buildah on PR, push on main.
**Depends on:** T1.

#### T10: CI E2E test workflow
Temporal + PostgreSQL as service containers. Run pytest E2E suite.

#### T11: TLS for Temporal connection
`TEMPORAL_TLS_ENABLED` + cert/key/CA env vars → `TLSConfig`.

#### T12: Upstream PR — executionResult context formatting
Add `executionResult` handling in sandbox's `_format_context_prefix()`.

**Fallback if not merged**: T8 documents the contract as-designed. T0 works without this change (executionResult context is sent but sandbox ignores it — degraded but functional). T18 tests against the documented contract, not the current sandbox behavior. When the PR merges, tests validate real behavior.

#### T13: Upstream PR — HTTP 502 for infra errors
Return HTTP 502 for ConnectionError/TimeoutError/RateLimitError.

**Fallback if not merged**: T0 treats all non-200 responses as infrastructure errors (current behavior). The 502 classification improves precision but isn't blocking — Temporal retries on any exception. When the PR merges, retry classification becomes correct.

#### T19: Workflow definition validation
Validate YAML at submission time in the API layer. Catch: circular conditions, undefined step references in `{{ steps.X.output }}`, duplicate `output_key`, missing required fields.

### P2 — Adoption / hardening

#### T14: /livez liveness probe
Process-health only — returns 200 whenever the workflow-runner process is alive and able to serve HTTP. Does NOT reflect worker idle/active state (that's metrics). An idle but healthy worker is still live.
#### T15: /readyz with Temporal connectivity check
#### T16: Podman compose update
#### T17: Temporal OTel interceptor
#### T18: Integration test for sandbox HTTP contract

## Dependencies

```
T8 (contract docs) ──── T0 (sandbox wiring), T18 (contract test)
T1 (Containerfile) ──┬── T6 (Helm), T9 (CI build), T16 (Podman)
T2 (tracing init) ───┬── T3 (spans) ── T17 (interceptor)
T4 (metrics) ────────── T5 (/metrics)
T12, T13 (upstream) — independent, submit early
```

## Execution Order (revised per review)

1. **Week 1**: T1, T2, T4, T8, T12*, T13* (*submit upstream PRs early)
2. **Week 2**: T0 (sandbox wiring), T3, T5, T9
3. **Week 3**: T10, T11, T6, T7
4. **Week 4**: T14-T18, T19 (validation)

## Verification

### Fast path (CI — every PR)
- `uv run pytest tests/unit/agents/ -q` — unit tests pass
- `uv run pytest tests/integration/temporal/ -v` — integration tests pass (WorkflowEnvironment)
- Container image builds successfully (T9)
- E2E with Temporal service container passes (T10)

### Kind-based smoke (weekly / pre-release)
- `helm install` on Kind cluster → all pods Ready
- Workflow-runner `/livez`, `/readyz`, `/metrics` respond correctly (`/healthz` retained as compatibility alias)
- NetworkPolicy: sandbox pod can reach LLM endpoint, cannot reach arbitrary hosts
- TLS: workflow-runner connects to Temporal with mTLS when configured
- Probes: `/livez` returns 200 when process alive; `/readyz` returns 200 when Temporal reachable, 503 otherwise
- Real sandbox E2E: spawn sandbox pod, call LLM, verify pod cleanup (T0)

### Production gate (before claiming production-ready)
- Non-stub E2E passes with real sandbox + real LLM
- Upstream PRs T12/T13 merged OR fallback behavior documented and tested
- Helm values reviewed for target environment
- Network policies verified in target namespace
