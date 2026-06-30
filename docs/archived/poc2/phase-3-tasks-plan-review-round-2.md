# Review: `phase-3-tasks.md`

## Findings

### 1. Blocker (Functionality): the Kind smoke verification gives `/livez` the wrong semantics

The verification section says `/livez` should return `503` when the worker is idle and `200` when active. That is not a liveness contract; it is workload-state reporting. A healthy but idle deployment would fail its own liveness probe and risk restart loops even though nothing is wrong.

Recommended fix:
- make `/livez` reflect only process health
- keep it `200` whenever the workflow-runner is alive enough to serve
- if you want worker-state reporting, put that behind `/readyz`, metrics, or a separate diagnostic endpoint instead of liveness

### 2. Major (Functionality): T0 uses the wrong sandbox health endpoint

T0 says `spawner.wait_ready(endpoint)` polls `/healthz` before calling the sandbox. But the companion architecture says the sandbox already exposes `GET /health` and `GET /ready`, and explicitly notes that a `/healthz` alias is not needed because the spawner probes `/health`.

That makes the core sandbox-wiring task depend on a stale endpoint contract. If implemented as written, the real wiring path can fail before any workflow step runs.

Recommended fix:
- change T0 to probe `/health`, or explicitly define and land a `/healthz` alias in the sandbox first
- keep the task text aligned with the existing sandbox contract so the non-stub path is implementable from the doc alone

### 3. Major (Quality): the new env-var contract is not backed by a concrete config schema or migration task

The doc now says the spawner must set all seven sandbox env vars from `ProviderConfig`, but the earlier phase contract only defines `ProviderConfig` with `name`, `model`, and `credentials_secret`. There is no task here that extends the schema, defines defaulting rules, or states where `provider_url`, `project`, `region`, and `api_version` live when they are needed.

That leaves a hidden second source of truth: either those values come from `ProviderConfig`, from deployment config, or from some other runtime layer, but the plan does not pin that down.

Recommended fix:
- define the authoritative schema for provider connection settings
- either extend `ProviderConfig` explicitly or state which values come from deployment config instead
- add a task or contract note for the migration/defaulting path so the seven-var contract is implementable

## Perspective Check

- Functionality: remaining gaps in probe semantics and the concrete sandbox readiness contract
- Quality: remaining gap in the source of truth for the expanded provider/env configuration
- Security: no new major issues found beyond the already-deferred single-team trust boundary, which is now stated explicitly

## Open Questions / Assumptions

- I assumed the verification bullets are intended to describe the real probe behavior, not just informal smoke-test observations.
- I assumed the existing `ProviderConfig` contract from Phase 1 is still authoritative unless this phase explicitly changes it.
- I assumed the sandbox health surface remains `/health` and `/ready` unless a new upstream change is planned.

## Summary

The revised plan resolved the original big gaps and is much closer, but it is not at `LGTM` yet. The remaining issues are smaller and more concrete: one stale sandbox endpoint, one incorrect liveness contract, and one under-specified source of truth for the expanded provider env configuration.
