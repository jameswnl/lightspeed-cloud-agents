# Phase 3 Tasks Review

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-06-29
**Context**: Reviewed after hands-on experience with the agentic operator + sandbox (deployed on Kind with GPT-5.5, submitted PRs, explored sandbox codebase in depth).

## Verdict: Approve with gaps

The phase is well-scoped and the dependency graph is clean. The execution order is realistic. But it's missing the core deliverable implied by the context statement ("no real sandbox pod has been spawned") — there's no task to actually wire the spawner to the sandbox.

## Gaps

### 1. No task to wire the spawner to the sandbox image

The phase context says "the entire sandbox execution path runs in stub mode — no real sandbox pod has been spawned." T8 documents the contract, T18 tests it, but **no task replaces the stub with real spawner calls**. The activity needs to:
- Spawn a sandbox pod (not the generic Pydantic AI runtime)
- Set `LIGHTSPEED_PROVIDER`, `LIGHTSPEED_MODEL`, and credentials via `SecretKeyRef`
- Call `POST /v1/agent/run` (not the current Pydantic AI `/v1/run` contract)
- Mount skills via OCI image volumes
- Handle the sandbox response format (`success`, `summary`, extra fields)

This is arguably the most important task in the phase — it's what makes the architecture real. Suggest adding as **T0: Wire spawner to sandbox contract** (P0, depends on T8).

### 2. No workflow cancellation endpoint

The architecture doc specifies `POST /workflows/:id/cancel` using Temporal's native cancellation. Not in this phase or the backlog. A user who triggers a high-risk workflow by mistake has no way to stop it. Suggest adding as P1.

### 3. No workflow definition validation

Invalid YAML (circular conditions, undefined step references in `{{ steps.X.output }}`, duplicate `output_key`) fails at runtime deep in the workflow. Should be caught at submission time in the API layer. Suggest adding as P1.

### 4. Upstream PRs (T12, T13) should start in week 1

These are scheduled for week 4 but depend on the sandbox team's review cycle, which is outside your control. Submit them in week 1 and work them in parallel with internal tasks. If the sandbox team takes 3 weeks to review, you're still on track. If you wait until week 4 to submit, the phase extends by however long review takes.

### 5. No task for spawner env var wiring

The sandbox's `config.py:resolve_sdk()` reads `LIGHTSPEED_PROVIDER`, `LIGHTSPEED_MODEL`, `LIGHTSPEED_MODEL_PROVIDER`, `LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, `LIGHTSPEED_PROVIDER_REGION`, `LIGHTSPEED_PROVIDER_API_VERSION`. The spawner needs to set all of these from the workflow config. This is part of gap #1 but worth calling out separately — the env var contract is non-trivial (7 vars, provider-dependent).

### 6. T6 (Helm chart) scope unclear

What does the Helm chart deploy?
- Option A: Just the workflow-runner + worker (assumes Temporal Server deployed separately)
- Option B: Full stack (Temporal Server + PostgreSQL + workflow-runner + worker)

Option A is simpler and lets teams use managed Temporal or an existing deployment. Option B is self-contained but adds operational burden. Should be decided and stated in the task.

## What's good

- Containerfile → Helm → CI is the right sequence
- OTel tracing and Prometheus metrics are P0 — correct priority for production
- Upstream PRs are separate tasks with no internal dependencies
- Network policies (T7) included — often forgotten
- TLS for Temporal connection (T11) included
- Liveness/readiness probes (T14/T15) included

## Suggested revised execution order

```
Week 1: T1, T2, T4, T8, T12*, T13*  (* submit upstream PRs early)
Week 2: T0 (sandbox wiring), T3, T5, T9
Week 3: T10, T11, T6, T7
Week 4: T14-T18, cancellation endpoint, YAML validation
```
