# PoC2 Phase 3: Productization

## Context

Phases 1-2 delivered the Temporal workflow engine with policy layer, LGTM'd after multiple review rounds. The entire sandbox execution path runs in stub mode — no real sandbox pod has been spawned. This phase makes it production-ready: container images, observability, Helm chart, sandbox contract, network hardening, and CI.

## Tasks

### P0 — Blocking

#### T1: Workflow-runner Containerfile
Create `deploy/workflow-runner/Containerfile`. Python 3.12-slim, uv sync, entrypoint `uvicorn agents.workflow.temporal_entrypoint:app`. Follow `deploy/agent-runtime/Containerfile` pattern.

#### T2: OTel tracing init in entrypoint
Call `init_tracing("workflow-runner")` in `temporal_entrypoint.py` lifespan. Reuse `src/agents/runtime/tracing.py`.

#### T3: Trace spans in activities
Wrap activities with OTel spans. Set span attributes (step_name, workflow_id, pod_name).
**Depends on:** T2.

#### T4: Prometheus metrics module
Create `src/agents/workflow/temporal_metrics.py` with workflow/step counters and histograms.

#### T5: /metrics endpoint
Add `/metrics` to workflow-runner FastAPI app.
**Depends on:** T4.

### P1 — Required for production

#### T6: Helm chart
Create `deploy/helm/cloud-agents-temporal/` with templates for all components.
**Depends on:** T1.

#### T7: Egress network policies
Workflow-runner → Temporal + sandbox + K8s API. Sandbox → LLM + DNS. Block rest.

#### T8: Sandbox contract documentation
Document POST `/v1/agent/run` request/response schema, env vars, error classification.

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

### P2 — Adoption / hardening

#### T14: /livez liveness probe
#### T15: /readyz with Temporal connectivity check
#### T16: Podman compose update
#### T17: Temporal OTel interceptor
#### T18: Integration test for sandbox HTTP contract

## Dependencies

```
T1 (Containerfile) ──┬── T6 (Helm), T9 (CI build), T16 (Podman)
T2 (tracing init) ───┬── T3 (spans) ── T17 (interceptor)
T4 (metrics) ────────── T5 (/metrics)
T8 (contract docs) ──── T18 (contract test)
T12, T13 (upstream) — independent
```

## Execution Order

1. Week 1: T1, T2, T4, T5, T8
2. Week 2: T3, T9, T10, T11
3. Week 3: T6, T7
4. Week 4: T12, T13
5. Week 5: T14-T18
