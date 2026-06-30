# Review: Phase 7 follow-up (`852d334b..7de5d284`)

## Findings

### 1. High: the new Kubernetes SA-token auth path still cannot authenticate real cross-pod calls
The new code switched both sides to local projected ServiceAccount tokens, but it still uses shared-secret equality semantics. In `src/agents/workflow/executor.py`, ephemeral workflow calls now send `RemoteAgentClient(endpoint, auth_token=get_api_token() or None)`, which reads the runner pod's local projected token. In `src/agents/runtime/auth.py`, the callee agent pod validates `Authorization` by comparing it to its own local `get_api_token()`, which now also falls back to `/var/run/secrets/tokens/agent-token`. Projected SA tokens are pod-specific, so the caller pod's token and callee pod's token will not match in the general case even when both pods use the same ServiceAccount and audience.

Why it matters:
- the Phase 7 Kubernetes auth implementation is still not functionally correct for real workflow traffic
- the live K8s trust boundary is still unresolved even though the code now looks closer to the approved design
- there is still no K8s test coverage that proves this path works end to end

Recommended fix:
- validate incoming Kubernetes bearer tokens with TokenReview or another cluster-verifiable mechanism instead of comparing against the callee pod's own token
- keep the shared-secret equality path only for explicit Podman / shared-secret mode
- add a K8s-auth test that exercises a real runner-pod -> spawned-agent-pod call in SA-token mode

### 2. High: orphan cleanup is still not wired into a reachable recovery contract
The new `RecoveryPoller` cleanup path still cannot reliably clean up real orphaned work. First, there is still no Phase 7 runtime code that writes a step result with `status="dispatched"`, so `RecoveryPoller` has nothing to detect; searching the workflow code still shows no producer for that state. Second, the poller reconstructs the resource name as `f"{step_result.step_name}-{hash}"` in `src/agents/workflow/advancement.py`, but the executor spawns resources as `f"{step.agent}-{hash}"` in `src/agents/workflow/executor.py`. Even if recovery were triggered, it would target the wrong resource name unless the step name happened to equal the agent name. The hashing logic also still hardcodes attempt `1`, so retry-sensitive recovery remains incomplete.

Why it matters:
- the crash-recovery cleanup path from Tasks 4-5 is still not actually reachable in practice
- orphaned Jobs can still leak after runner failure
- the implementation still lacks a persisted source of truth for reconstructing the correct resource to destroy

Recommended fix:
- persist a real dispatched step state before control leaves the runner
- persist enough metadata for recovery to destroy the exact spawned resource (at minimum agent identity or full spawned name, plus retry attempt)
- make recovery use that persisted metadata rather than reconstructing from `step_name`

### 3. Medium: the new Podman pytest E2E suite still fails under the standard invocation it claims to support
The new `tests/e2e/test_phase7_podman_pytest.py` file is a real pytest module, but it does not pass here under normal `pytest`. Its module-scoped fixture calls `asyncio.get_event_loop().run_until_complete(...)`, which raises `RuntimeError: There is no current event loop in thread 'MainThread'` on Python 3.13. The suite also emits `PytestUnknownMarkWarning` for the unregistered `e2e` mark. So the commit message's claim that these tests "all pass under standard pytest invocation" is not currently true in this environment.

Why it matters:
- the phase's strongest new verification claim is overstated
- the new E2E suite is not yet portable across the project's supported pytest/runtime environment
- there is still no automated test covering the K8s SA-token path above

Recommended fix:
- switch the fixture to an explicit event-loop strategy compatible with current Python/pytest behavior (for example `asyncio.run(...)` or a dedicated loop fixture)
- register the `e2e` marker in pytest config
- rerun and report the exact standard command once it passes

## Perspective Check
- Functionality: remaining gaps. The new K8s auth and recovery cleanup implementations still do not satisfy the intended runtime contract.
- Quality: remaining gaps. The new pytest E2E suite does not pass under the claimed standard invocation.
- Security: remaining gaps. The Kubernetes production auth path is still not safely or correctly validated for cross-pod calls.

## Verification
- Reviewed the new follow-up commit and current Phase 7 state:
  - `src/agents/runtime/auth.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `src/agents/workflow/advancement.py`
  - `src/agents/workflow/executor.py`
  - `tests/e2e/test_phase7_podman_pytest.py`
  - `tests/unit/agents/workflow/test_advancement.py`
- Checked the change scope since the last review:
  - `git diff --name-only 852d334beecb70c685d51576cfe21502519e6dd7..HEAD`
- Confirmed the recovery trigger is still missing:
  - searched the workflow code for a producer of `status="dispatched"` and found none
- Ran focused Phase 7 verification:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py tests/e2e/test_phase7_podman_pytest.py -q`
  - Result: `65 passed, 4 errors`
  - The four errors all come from `tests/e2e/test_phase7_podman_pytest.py` fixture setup on Python 3.13 (`RuntimeError: There is no current event loop in thread 'MainThread'`)

## Summary
Not `LGTM` yet. This follow-up commit made real progress toward the prior review items, but two of the three claimed fixes are still incomplete in live behavior: the new Kubernetes auth path still uses local-token equality across pods, and the recovery cleanup path still has no reachable dispatched-state producer and reconstructs the wrong resource name. The new Podman pytest suite is a good direction, but it does not yet pass under the standard pytest invocation it claims to support.
