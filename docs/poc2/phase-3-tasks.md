# PoC2 Phase 3: Productization

## Context

Phases 1-2 delivered the Temporal workflow engine with policy layer, LGTM'd after multiple review rounds. The entire sandbox execution path runs in stub mode — no real sandbox pod has been spawned. This phase makes it production-ready: container images, observability, Helm chart, sandbox contract, network hardening, CI, and — critically — wires the spawner to the real sandbox.

## Contracts (from plan review round 1)

### Sandbox Env Var Contract

The sandbox's `config.py:resolve_sdk()` reads these env vars. The spawner must set all of them from the workflow definition's `ProviderConfig`:

| Env Var | Source | Required |
|---------|--------|----------|
| `LIGHTSPEED_PROVIDER` | `provider.name` | Yes |
| `LIGHTSPEED_MODEL` | `provider.model` | Yes |
| `LIGHTSPEED_MODEL_PROVIDER` | Derived from provider name | Provider-dependent |
| `LIGHTSPEED_PROVIDER_URL` | Provider endpoint URL | Provider-dependent |
| `LIGHTSPEED_PROVIDER_PROJECT` | GCP project for Vertex | Gemini only |
| `LIGHTSPEED_PROVIDER_REGION` | GCP region for Vertex | Gemini only |
| `LIGHTSPEED_PROVIDER_API_VERSION` | API version override | Optional |

Credential env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) are resolved from `credentials_secret` via K8s `SecretKeyRef` or Podman host env propagation.

### Helm Chart Scope

**Option A (chosen)**: Workflow-runner + worker only. Assumes Temporal Server deployed separately (managed Temporal, existing deployment, or separate Helm release). This keeps the chart focused and avoids coupling to Temporal Server lifecycle.

The chart includes: workflow-runner Deployment + Service, RBAC, NetworkPolicies, ConfigMap for defaults. Temporal Server connection via `values.yaml` settings.

### Workflow Cancellation

`POST /v1/workflows/{id}/cancel` already exists in `temporal_api.py`. No new task needed — already implemented in Phase 1.

## Tasks

### P0 — Blocking

#### T0: Wire spawner to real sandbox contract
Replace stub activity path with real spawner calls. This is the core deliverable:
- Spawn sandbox pod (using `lightspeed-agentic-sandbox` image, not generic agent-runtime)
- Set full env var contract (7 vars from ProviderConfig + credential SecretKeyRef)
- Call `POST /v1/agent/run` (sandbox contract, not Pydantic AI `/v1/run`)
- Parse sandbox response format (`success`, `summary`, extra fields)
- Handle skills mount via OCI image volumes

**Depends on:** T8 (contract documented first).

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

#### T13: Upstream PR — HTTP 502 for infra errors
Return HTTP 502 for ConnectionError/TimeoutError/RateLimitError.

#### T19: Workflow definition validation
Validate YAML at submission time in the API layer. Catch: circular conditions, undefined step references in `{{ steps.X.output }}`, duplicate `output_key`, missing required fields.

### P2 — Adoption / hardening

#### T14: /livez liveness probe
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
