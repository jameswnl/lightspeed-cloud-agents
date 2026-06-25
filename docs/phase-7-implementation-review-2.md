# Review: Phase 7 follow-up commit (`884d7766`)

## Findings

### 1. High: the Kubernetes auth model from the approved Phase 7 design is still not implemented
The follow-up commit fixed `PermissionScope` runtime enforcement and partially wired `derive_status()`, but it did not touch the Phase 7 Kubernetes auth path. The implementation still uses only `AGENT_API_TOKEN`-style shared-secret auth: `BearerAuthMiddleware` reads from env, `KubernetesSpawner` still does not mount a projected ServiceAccount token volume, and the ephemeral workflow path still creates `RemoteAgentClient(endpoint)` with no `auth_token`. The Kind E2E tests also still exercise the shared-secret model, not the approved K8s SA-token model.

Why it matters:
- the branch still does not implement the approved production trust boundary for Kubernetes
- K8s ephemeral pods are not authenticated with per-pod identity on the live workflow path
- the current tests still provide confidence about the old auth model rather than the one Phase 7 committed to ship

Recommended fix:
- implement projected SA token volume wiring in `KubernetesSpawner`
- teach the workflow/executor path to read and pass the K8s token into `RemoteAgentClient`
- replace or extend `BearerAuthMiddleware` with the planned Kubernetes validation path
- update K8s E2E coverage to exercise SA-token mode, not only `AGENT_API_TOKEN`

### 2. High: the remaining robustness/runtime wiring gaps are still unresolved outside `derive_status()`
This commit does meaningfully improve two things: `PermissionScope.effective_tools()` is now called in the runtime path, and `WorkflowExecutor.get_state()` now re-derives status from step results. But the broader robustness finding from the first review is still only partially resolved. `RecoveryPoller` still marks orphaned dispatched steps failed without destroying the backing Job, `StepDispatcher` still uses UUID-based ephemeral names and still passes `OPENAI_API_KEY` as a literal env var, and the executor’s hash-based spawn naming still hardcodes attempt `1` instead of the real retry attempt.

Why it matters:
- the main crash/retry cleanup semantics approved in Phase 7 are still only partly wired into runtime behavior
- orphaned work can still leak because the recovery path does not actually clean up spawned resources
- retry-sensitive, reconstructible naming is still not consistently implemented across dispatch paths

Recommended fix:
- extend recovery to call `spawner.destroy(...)` for orphaned running work
- unify spawn naming around the approved reconstructible hash contract, including retry attempt
- stop passing literal secrets in the async/ephemeral dispatch path

### 3. Medium: the E2E suite still does not run as meaningful pytest automation
The updated unit tests now pass, including the new `PermissionScope` coverage, but the E2E layer is unchanged and still fails under the advertised `pytest` invocation. `tests/e2e/test_phase7_security.py` remains a script with `main()` and no real pytest tests, while `tests/e2e/test_phase7_kind.py` still assumes setup from `main()` rather than pytest fixtures. Re-running the same E2E command still produces two Kind failures and no real Podman pytest coverage.

Why it matters:
- the branch still overstates its automated end-to-end verification
- CI-style `pytest` runs do not yet prove the K8s/Podman phase claims
- this leaves the remaining auth and runtime gaps under-validated

Recommended fix:
- convert the Podman and Kind E2E files into real pytest tests/fixtures, or explicitly keep them as scripts and stop treating them as pytest coverage
- make setup/teardown part of pytest fixtures so the K8s tests are runnable in automation
- only claim E2E verification once the suite passes in the same invocation style reviewers are expected to use

## Perspective Check
- Functionality: remaining gaps. `derive_status()` and tool filtering are improved, but the recovery/cleanup runtime contract is still incomplete.
- Quality: remaining gaps. The targeted unit suite is stronger, but the E2E path is still not reliable automation.
- Security: remaining gaps. The approved Kubernetes auth model is still not implemented on the live workflow path.

## Verification
- Reviewed follow-up commit `884d7766`
- Read the updated files and prior review together:
  - `src/agents/runtime/generic_runner.py`
  - `src/agents/workflow/executor.py`
  - `tests/unit/agents/test_phase7_robustness.py`
  - `docs/design/cloud-agents/phase-7-implementation-review-1.md`
- Re-checked relevant unchanged runtime files for remaining issues:
  - `src/agents/runtime/auth.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `src/agents/workflow/advancement.py`
  - `src/agents/workflow/step_dispatcher.py`
- Ran updated focused unit tests:
  - `uv run pytest tests/unit/agents/test_phase7_robustness.py tests/unit/agents/test_phase7_security.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py -q`
  - Result: `65 passed`
- Re-ran advertised E2E pytest paths:
  - `uv run pytest tests/e2e/test_phase7_security.py tests/e2e/test_phase7_kind.py -q`
  - Result: `2 failed, 1 passed`
  - Kind tests still fail because setup only happens in `main()`, not pytest fixtures
  - Podman E2E file still does not contribute real pytest test coverage

## Summary
Not LGTM yet. `884d7766` resolves one of the prior major findings for real: `PermissionScope` is now enforced in the runtime path, and there is new passing unit coverage for it. But the Phase 7 Kubernetes auth model is still not implemented, the remaining recovery/dispatch robustness contracts are still only partially wired, and the E2E suite is still not meaningful pytest automation.
