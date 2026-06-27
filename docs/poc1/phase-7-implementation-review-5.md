# Review: Phase 7 whole-scope follow-up (`57726e48..c6efd57e`)

## Findings

### 1. High: the final Phase 7 auth implementation no longer matches the approved Kubernetes production design
The latest commit intentionally reverts Kubernetes auth to the shared `AGENT_API_TOKEN` model, but the approved Phase 7 task doc still requires Kubernetes production auth to use projected ServiceAccount tokens plus cluster-side validation. The current code in `src/agents/runtime/auth.py` now explicitly says both Podman and K8s use the same shared env token, and `src/agents/spawner/kubernetes_spawner.py` no longer mounts projected SA tokens. That may be a reasonable pragmatic rollback from the broken pod-specific-token comparison attempt, but it is still a scope/design change, not a completion of the approved Task 3 contract.

Why it matters:
- the whole-batch implementation still does not match the approved production trust boundary for Kubernetes
- the phase task doc says the critical security issues must be fixed before production, but this one is now deferred in code rather than completed
- writing `LGTM` would bless a different security model than the one this phase was approved to ship

Recommended fix:
- either implement the approved K8s production path properly using TokenReview (or equivalent cluster validation), or
- explicitly update the Phase 7 task/design docs to narrow the shipped scope and move Kubernetes per-pod identity to a follow-up phase before asking for `LGTM`

### 2. Medium: the new Podman pytest E2E still does not cleanly prove the intended contract
The pytest conversion is better than before, but the suite still failed in the standard command I ran: `74 passed, 1 failed`. The remaining failure is `tests/e2e/test_phase7_podman_pytest.py::TestBearerAuth::test_authenticated_succeeds`, where the authenticated request returns HTTP 200 but `success=False` because the agent run itself fails with `UnexpectedModelBehavior: Exceeded maximum output retries (3)`. That means the test is still coupled to successful agent/model execution rather than only proving the auth contract it claims to test.

Why it matters:
- the strongest new verification claim still overstates what is actually proven
- this test can fail for model/output reasons unrelated to bearer-auth correctness
- the suite still does not provide a robust, caller-side proof that "authenticated request is accepted"

Recommended fix:
- make the auth E2E assert the auth boundary directly, for example by checking that authenticated requests are no longer rejected with 401 and by using a deterministic test agent or mocked success path
- keep model-behavior verification separate from auth verification

### 3. Medium: several whole-phase deliverables in the approved task doc are still not reflected in committed implementation assets
On this full-batch pass, a few planned artifacts are still missing from the committed scope. The approved task doc still calls for Kubernetes `AlreadyExists` handling and workflow visibility labels in `src/agents/spawner/kubernetes_spawner.py`, plus a dedicated integration test file at `tests/integration/agents/test_executor_spawner.py`. I do not see those implementation artifacts in the current Phase 7 tree. Some behavior is covered elsewhere now, but the committed assets still do not line up cleanly with the approved task list.

Why it matters:
- the phase still has doc-to-implementation drift even after the latest fix batch
- reviewers cannot reliably tell which Task 4/5/10 items were intentionally descoped versus accidentally left out
- this weakens the “phase complete” claim even if the remaining code paths are mostly functional

Recommended fix:
- either land the missing implementation/test assets, or
- update `docs/design/cloud-agents/phase-7-tasks.md` so the shipped scope accurately describes what Phase 7 now includes and what moved out of scope

## Perspective Check
- Functionality: remaining gaps. The implemented Kubernetes auth model still differs from the approved whole-phase contract.
- Quality: remaining gaps. The new auth E2E is improved but still does not cleanly prove the intended contract, and the task doc still drifts from committed assets.
- Security: remaining gaps. The approved Kubernetes production trust boundary is still not what the current code implements.

## Verification
- Performed a whole-scope re-review over the Phase 7 implementation range:
  - `57726e48..c6efd57e`
- Re-read the approved phase scope and latest prior review:
  - `docs/design/cloud-agents/phase-7-tasks.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-4.md`
- Re-checked the relevant current implementation:
  - `src/agents/runtime/auth.py`
  - `src/agents/workflow/executor.py`
  - `src/agents/workflow/advancement.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `tests/e2e/test_phase7_podman_pytest.py`
  - `pyproject.toml`
- Checked for whole-phase artifacts still expected by the task doc:
  - searched for `test_executor_spawner` and found no committed integration test file
  - searched `src/agents/spawner/kubernetes_spawner.py` for the planned labels / `AlreadyExists` handling and found no matching implementation
- Ran focused verification including the new pytest E2E:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py tests/e2e/test_phase7_podman_pytest.py -q`
  - Result: `74 passed, 1 failed`
  - Failure: `tests/e2e/test_phase7_podman_pytest.py::TestBearerAuth::test_authenticated_succeeds`
  - Observed failure detail: authenticated request was accepted at HTTP level (`200`) but the agent run returned `success=False` with `UnexpectedModelBehavior: Exceeded maximum output retries (3)`

## Summary
Not `LGTM` yet. The latest commit fixes the immediate bugs from review 4, but on a required whole-phase pass the implementation still diverges from the approved Kubernetes production auth design, the new auth E2E still does not robustly prove the claimed contract, and the Phase 7 task doc still overstates or misstates some shipped assets. To get to `LGTM`, the code and the approved phase scope need to line up again, either by finishing the approved K8s production auth model or by explicitly narrowing/updating the Phase 7 design and task docs.
