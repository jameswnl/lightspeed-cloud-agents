# Review: Phase 7 implementation follow-up (`57726e48..852d334b`)

## Findings

### 1. High: the approved Kubernetes auth model is still not implemented on the workflow path
Phase 7's approved task doc requires Kubernetes production auth to use projected ServiceAccount tokens plus Kubernetes-side validation, but the code at `HEAD` still only implements the shared-secret `AGENT_API_TOKEN` model. `BearerAuthMiddleware` only checks a static env token, `KubernetesSpawner` does not mount a projected token volume, and the executor still creates `RemoteAgentClient(endpoint)` with no `auth_token` for ephemeral workflow calls. The Kind E2E coverage also deploys `AGENT_API_TOKEN` via Secret, so it validates the old shared-secret path instead of the approved Kubernetes trust boundary.

Why it matters:
- the main production security requirement in Task 3 remains open
- in-cluster ephemeral workflow calls still do not have per-pod identity
- the current K8s test coverage gives confidence about the deferred model, not the approved one

Recommended fix:
- add projected ServiceAccount token volume wiring in `src/agents/spawner/kubernetes_spawner.py`
- teach the workflow executor path to read and pass that token into `RemoteAgentClient`
- extend `src/agents/runtime/auth.py` with the planned Kubernetes validation path instead of only `AGENT_API_TOKEN`
- update the Kind test to exercise SA-token mode rather than shared-secret mode

### 2. High: the recovery/idempotency contract from Tasks 4-5 is still only partially implemented
Some Phase 7 pieces landed, but the end-to-end recovery path described in the task doc is not wired. `WorkflowExecutor` now hashes spawned names, but it hardcodes `attempt=1`, so retry-sensitive naming is still wrong. `RecoveryPoller` looks only for steps already marked `"dispatched"` and only marks them failed, but no code in the phase writes a `"dispatched"` step result at all, and the poller never calls `spawner.destroy(...)` to clean up orphaned Jobs. `KubernetesSpawner` also still creates Jobs directly without the planned `AlreadyExists` idempotency handling or the visibility labels (`workflow-id`, `step-name`, `created-at`) called for in Task 5.

Why it matters:
- the crash-recovery path the phase depends on is still not reachable in practice
- reconstructible naming is incomplete for retries
- orphaned spawned work can still leak because recovery never destroys backing resources

Recommended fix:
- write the dispatched state into workflow state before handing control to spawned work
- make `RecoveryPoller` call `spawner.destroy(...)` after claiming an orphaned step
- use the real retry attempt when computing hashed spawn names
- add the planned labels and `AlreadyExists` handling in `src/agents/spawner/kubernetes_spawner.py`

### 3. Medium: the advertised Phase 7 deployment verification is still weaker than claimed
The focused unit coverage is good, but the deployment-path verification is still not reliable automation. `tests/e2e/test_phase7_security.py` is still a standalone script with `main()`, so `pytest` does not execute any Podman E2E tests from it. `tests/e2e/test_phase7_kind.py` contains pytest-style test functions, but they depend on setup performed only inside `main()`, so the advertised pytest invocation still fails immediately unless the cluster was created out-of-band first. The task doc also called for an executor+spawner integration test file, but the implementation still relies on unit-style coverage instead of the promised dedicated integration test.

Why it matters:
- the branch still overstates how much deployment-path behavior is automatically verified
- review confidence for the remaining security and recovery claims is lower than the docs imply
- regressions in the real spawn/auth path are still easy to miss

Recommended fix:
- either convert the Podman and Kind files into real pytest automation with fixtures, or clearly document them as manual scripts and stop counting them as automated E2E verification
- add the promised executor+spawner integration coverage for dispatch, retry, and cleanup behavior

## Perspective Check
- Functionality: remaining gaps. The recovery and retry/idempotency behavior described in Tasks 4-5 is still only partially wired.
- Quality: remaining gaps. Focused unit tests pass, but the deployment-path verification is still weaker than the phase docs claim.
- Security: remaining gaps. The approved Kubernetes auth model from Task 3 is still not implemented on the live workflow path.

## Verification
- Reviewed the Phase 7 scope on branch `cloud-agents`: `57726e48..852d334b`
- Re-read the phase task doc and prior follow-up reviews:
  - `docs/design/cloud-agents/phase-7-tasks.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-1.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-2.md`
- Re-checked the current implementation and tests:
  - `src/agents/runtime/auth.py`
  - `src/agents/remote_agent_client.py`
  - `src/agents/runtime/generic_runner.py`
  - `src/agents/spawner/base.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `src/agents/workflow/executor.py`
  - `src/agents/workflow/advancement.py`
  - `src/agents/workflow/definition.py`
  - `src/agents/workflow/auto_approve.py`
  - `src/agents/workflow/persistence.py`
  - `src/agents/workflow/state.py`
  - `src/agents/workflow/step_dispatcher.py`
  - `tests/unit/agents/test_phase7_security.py`
  - `tests/unit/agents/test_phase7_robustness.py`
  - `tests/unit/agents/workflow/test_auto_approve.py`
  - `tests/unit/agents/workflow/test_definition_api.py`
  - `tests/unit/agents/workflow/test_step_dispatcher.py`
  - `tests/unit/agents/spawner/test_kubernetes_spawner.py`
  - `tests/unit/agents/spawner/test_podman_spawner.py`
  - `tests/e2e/test_phase7_security.py`
  - `tests/e2e/test_phase7_kind.py`
- Ran focused unit tests:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py -q`
  - Result: `65 passed`
- Ran the advertised Phase 7 E2E pytest paths:
  - `uv run pytest tests/e2e/test_phase7_security.py tests/e2e/test_phase7_kind.py -q`
  - Result: `2 failed, 1 passed`
  - `tests/e2e/test_phase7_security.py` still does not provide real pytest coverage
  - `tests/e2e/test_phase7_kind.py` still fails under pytest because setup lives in `main()`

## Summary
Not `LGTM` yet. Phase 7 made real progress: explicit `risk_level` is fail-closed, `PermissionScope` is now enforced at runtime, `derive_status()` is used on load, secretKeyRef support exists, and the focused unit suite passes. But the approved Kubernetes auth model is still deferred, and the recovery/idempotency path from Tasks 4-5 is still not fully wired into runtime behavior. The deployment-path verification is also still weaker than the phase documentation currently suggests.
