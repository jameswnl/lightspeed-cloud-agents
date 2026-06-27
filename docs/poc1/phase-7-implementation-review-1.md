# Review: Phase 7 implementation (`3ee2fa23..b454b3de`)

## Findings

### 1. High: the Kubernetes auth model from the approved Phase 7 design is still not implemented
The approved plan moved Kubernetes production auth to projected ServiceAccount tokens plus TokenReview validation, but the implementation still only supports a static `AGENT_API_TOKEN` shared secret. `BearerAuthMiddleware` reads only `AGENT_API_TOKEN` from the environment, `KubernetesSpawner` never mounts a projected token volume, and `WorkflowExecutor` still creates ephemeral `RemoteAgentClient(endpoint)` instances with no `auth_token` at all. The new Kind E2E test also deploys `AGENT_API_TOKEN` via Secret, so it verifies the old shared-secret path rather than the approved K8s trust boundary.

Why it matters:
- the branch does not actually deliver the K8s production auth hardening Phase 7 was approved to implement
- ephemeral K8s pods are still not authenticated with per-pod identity on the actual workflow path
- the tests currently give false confidence by exercising a different auth model than the one the plan settled on

Recommended fix:
- implement projected SA token volume wiring in `KubernetesSpawner`
- teach the workflow/executor path to read and pass the K8s token into `RemoteAgentClient`
- replace or extend `BearerAuthMiddleware` with the planned Kubernetes validation path
- update the K8s E2E coverage to exercise SA-token mode, not only `AGENT_API_TOKEN`

### 2. High: multiple Phase 7 robustness fixes landed as helpers/tests, but are still not wired into the runtime path
The branch added `WorkflowState.derive_status()` and tests around content-hash naming, but the live execution path still does not use those contracts. `derive_status()` is never called anywhere outside its definition. `RecoveryPoller` still only marks orphaned dispatched steps failed and never destroys backing Jobs. `StepDispatcher` still uses UUID-based ephemeral names and injects `OPENAI_API_KEY` as a literal env var. `WorkflowExecutor` moved to a hash-based name, but hardcodes `attempt=1`, so the approved retry-sensitive naming contract is still not actually implemented.

Why it matters:
- the implementation claims "security hardening + robustness complete," but the main crash/retry cleanup semantics are still partial
- workflow status can still drift because `status` remains manually mutated everywhere
- the stateless cleanup/recovery path does not yet match the approved design for reconstructible names and orphan cleanup

Recommended fix:
- make executor/load/resume paths actually derive workflow status from step results on load/resume/update
- extend recovery to call `spawner.destroy(...)` for orphaned running work
- unify spawn naming in the real dispatch path around the approved reconstructible hash contract, including retry attempt
- stop passing literal secrets in the async/ephemeral dispatch path

### 3. High: `PermissionScope` is still a no-op at runtime
Phase 7 added `permissions` to `WorkflowStepSpec`, and the executor now forwards `allowed_tools` / `denied_tools` in request context, but `generic_runner` still ignores them. The only `effective_tools()` call in the repo is the method definition itself. So the schema now advertises per-step tool restrictions that the runtime does not enforce.

Why it matters:
- this is a security boundary the implementation claims to support but does not actually enforce
- workflows can declare `permissions` and still run with the full tool set
- the tests passing here are misleading because they validate models and helpers, not actual enforcement

Recommended fix:
- validate and pass a typed permissions object to the runner
- call `PermissionScope.effective_tools()` during tool registration in `create_generic_runner()`
- add an integration test that proves a denied tool is unavailable at runtime, not just present in context

### 4. Medium: the new E2E coverage does not currently run as a meaningful automated test suite
The focused unit suite passed, but the advertised E2E coverage is much weaker than it looks. `tests/e2e/test_phase7_security.py` is a script with `main()` and no pytest tests, so `pytest` does not execute its Podman checks at all. `tests/e2e/test_phase7_kind.py` only creates the cluster and deploys fixtures from `main()`, so running it under `pytest` fails immediately because the test functions assume that setup already happened. I ran the E2E files with `pytest` and got two failures from the Kind file and zero actual Podman test collection.

Why it matters:
- the branch's "comprehensive tests" claim is overstated for the actual automated verification path
- CI-style `pytest` runs do not currently prove the K8s/Podman end-to-end claims
- this hid the implementation gaps above because the strongest deployment-path checks are not really being exercised

Recommended fix:
- convert the Podman and Kind E2E files into real pytest tests/fixtures, or explicitly keep them as scripts and stop presenting them as pytest coverage
- make the setup/teardown part of pytest fixtures so the K8s tests are runnable in automation
- only claim E2E verification once the suite passes in the same invocation style reviewers are expected to use

## Perspective Check
- Functionality: remaining gaps. Several approved runtime contracts, especially around recovery/state derivation, are only partially wired.
- Quality: remaining gaps. The unit suite is strong for helpers, but the E2E path is not yet reliable automation.
- Security: remaining gaps. The K8s production auth model and per-step permission enforcement are not actually implemented on the live workflow path.

## Verification
- Reviewed the Phase 7 implementation range on branch `cloud-agents`: `3ee2fa23..b454b3de`
- Read the touched runtime files and matching tests together:
  - `src/agents/remote_agent_client.py`
  - `src/agents/spawner/base.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `src/agents/spawner/podman_spawner.py`
  - `src/agents/workflow/api.py`
  - `src/agents/workflow/auto_approve.py`
  - `src/agents/workflow/definition.py`
  - `src/agents/workflow/definition_store.py`
  - `src/agents/workflow/executor.py`
  - `src/agents/workflow/step_dispatcher.py`
  - `src/agents/workflow/advancement.py`
  - `src/agents/workflow/persistence.py`
  - `src/agents/workflow/state.py`
  - `src/agents/workflow/permissions.py`
  - `src/agents/runtime/generic_runner.py`
  - `src/agents/runtime/auth.py`
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
  - Result: `63 passed`
- Ran advertised E2E pytest paths:
  - `uv run pytest tests/e2e/test_phase7_security.py tests/e2e/test_phase7_kind.py -q`
  - Result: `2 failed, 1 passed`
  - Kind tests failed because cluster/setup only happens in `main()`, not pytest fixtures
  - Podman E2E file did not contribute real pytest test coverage

## Summary
Not LGTM yet. The branch adds a lot of useful scaffolding and test helpers, but the actual workflow/runtime path still does not implement the approved Kubernetes auth model, does not enforce `PermissionScope`, and does not fully wire the Phase 7 robustness contracts into recovery/state handling. The unit tests are better than before, but the E2E layer is not yet proving the phase claims in an automated way.
