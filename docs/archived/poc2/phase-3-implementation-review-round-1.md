# Review: PoC2 Phase 3 implementation (current working tree)

## Findings

### 1. Blocker (Functionality): the new workflow-runner image does not boot because its uvicorn target does not exist

`deploy/workflow-runner/Containerfile` starts `uvicorn agents.workflow.temporal_entrypoint:app`, but `src/agents/workflow/temporal_entrypoint.py` only defines `build_temporal_app()` and never exports a module-level `app`. The image builds, but a real container exits immediately with `Error loading ASGI app. Attribute "app" not found in module "agents.workflow.temporal_entrypoint"`. That means T1's core deliverable is not runnable yet.

Recommended fix:
- either export `app = build_temporal_app()` in `temporal_entrypoint.py`
- or switch the container entrypoint to `uvicorn agents.workflow.temporal_entrypoint:build_temporal_app --factory`
- keep a runtime smoke test that uses a random published port and fails with container logs when startup breaks

### 2. Blocker (Functionality): the real sandbox path still waits on `/healthz`, but the phase-3 contract requires `/health`

Phase 3's T0 contract explicitly says sandbox readiness must use `/health`, not `/healthz`. The current activity path still calls `spawner.wait_ready(endpoint)` with no override, and `AgentSpawner.wait_ready()` hardcodes `GET {endpoint}/healthz`. On a real `lightspeed-agentic-sandbox` pod this will never become ready even if the pod is healthy, so the core spawn -> wait -> call flow will fail before the first request.

Recommended fix:
- plumb `health_path="/health"` through the sandbox activity path
- or update `SpawnConfig.health_path` and make `wait_ready()` respect it
- add a regression test that proves the Temporal activity waits on `/health` for sandbox-backed steps

### 3. Major (Security): the provider env/credential contract from the approved phase-3 plan is still not implemented

The approved plan requires the workflow-runner to forward deployment-level provider settings (`LIGHTSPEED_MODEL_PROVIDER`, `LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, `LIGHTSPEED_PROVIDER_REGION`, `LIGHTSPEED_PROVIDER_API_VERSION`) and to resolve credentials from `ProviderConfig.credentials_secret` via `SecretKeyRef` on Kubernetes or host-env propagation on Podman. The current `run_sandbox_step()` only forwards `LIGHTSPEED_PROVIDER`, `LIGHTSPEED_MODEL`, and an optional `LIGHTSPEED_SERVICE_ACCOUNT`. I could not find any runtime use of the required deployment env vars or any secret resolution path keyed off `credentials_secret`.

This is both a functionality and trust-boundary gap: non-default providers will start without the required configuration, and secrets handling is still implicit instead of enforced by the runner.

Recommended fix:
- read the five deployment-scoped env vars from the workflow-runner environment and forward them explicitly when present
- resolve `credentials_secret` into the concrete credential env var expected by the selected provider
- implement the Kubernetes `SecretKeyRef` / Podman env propagation split described in the plan
- add activity/spawner tests that assert both the forwarded provider env and the credential injection path

### 4. Major (Quality): the current tests still do not verify the core phase-3 deliverable

The phase-3 task list requires a non-stub E2E that proves real sandbox pod creation, readiness, HTTP execution, cleanup, and 502 retry behavior. `tests/e2e/temporal/test_temporal_e2e.py` still states that the sandbox activity runs in stub mode and explicitly says it does **not** prove the spawn -> HTTP -> destroy path. The new `tests/e2e/temporal/test_container_build.py` only checks image build/startup basics, hardcodes host port `18080`, and therefore failed in my run before it even exercised the container health endpoint. That leaves the most important phase-3 seam effectively unverified.

Recommended fix:
- add the non-stub Kind/Podman E2E required by T0
- verify pod/container creation and cleanup explicitly
- add a failing-sandbox case that proves HTTP 502 triggers Temporal retry behavior
- make the container-build test use a random published port so it fails on real runner bugs instead of local port collisions

## Perspective Check
- Functionality: not covered well enough yet; the workflow-runner container does not start, and the sandbox readiness path still targets the wrong health endpoint.
- Quality: remaining gaps; the tests pass for unit-level behavior, but the core phase-3 runtime seam is still unproven.
- Security: remaining gaps; provider-specific deployment env and credential injection are still not wired through the runner/spawner path.

## Verification
- `uv run pytest tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_entrypoint.py -q` -> `25 passed`
- `uv run pytest tests/e2e/temporal/test_container_build.py -q` -> `2 passed, 1 error`; failed because the test hardcodes host port `18080`, which was already in use
- Manual smoke check:
  - `podman run -d --name review-workflow-runner -P workflow-runner:test`
  - container exited immediately
  - `podman logs review-workflow-runner` -> `Error loading ASGI app. Attribute "app" not found in module "agents.workflow.temporal_entrypoint"`
- Code inspection of the sandbox path confirmed:
  - `run_sandbox_step()` calls `spawner.wait_ready(endpoint)` with no sandbox-specific health path override
  - `AgentSpawner.wait_ready()` hardcodes `/healthz`
  - no runtime references were found for the phase-3 provider env vars or `credentials_secret` resolution

## Summary

The current phase-3 implementation is not review-ready yet. The highest-risk issues are a non-booting workflow-runner image, a readiness probe mismatch that breaks real sandbox execution, and an unimplemented provider credential/configuration contract. Even where unit tests pass, the tests still do not prove the core phase-3 spawn-and-execute path.
