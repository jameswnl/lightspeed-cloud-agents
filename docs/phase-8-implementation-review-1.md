# Review: Phase 8 implementation

## Findings

### 1. High: the deployed recovery poller cannot recover or advance async workflows
`build_workflow_app()` and `build_stateless_app()` start `RecoveryPoller(persistence)` with no `spawner`, no `client_factory`, and no `dispatcher` in `src/agents/workflow/entrypoint.py`. That makes the production poller unable to poll agent pods, destroy spawned resources, or redispatch follow-on work. The problem is reinforced inside `RecoveryPoller._poll_once()`, which calls `advance_workflow(self._persistence, None, wf.workflow_id)` after a recovered result, so even a successfully recovered callback-loss case cannot dispatch the next agent step.

This breaks the main Phase 8 contract that the recovery poller is the fallback path for lost callbacks and replica crashes. In the shipped app, a missed callback will degrade into timeout/failure behavior instead of resuming the workflow on another replica.

Recommended fix: instantiate the poller with the same `spawner`, `client_factory`, and dispatcher context the executor uses, and pass a real dispatcher into `advance_workflow()` from the poller recovery path.

### 2. High: `dispatch_async()` does not implement the advertised CAS-backed persist-before-spawn contract
The phase doc says async dispatch must persist a durable recovery handle with CAS before spawn, then update `run_id` with CAS after submission. The actual implementation in `src/agents/workflow/step_dispatcher.py` does two plain `persistence.save(...)` writes, never uses `save_with_version()`, and persists `output["endpoint"] = None` before spawn. That has two concrete consequences:

- multi-replica advancement is not first-writer-wins; two replicas can race through `advance_workflow()` and both call `dispatch_async()` because there is no CAS claim around the step transition to `dispatched`
- the pre-spawn crash record is missing the endpoint the design says another replica needs in order to re-submit async work, so the recovery handle is weaker than the documented contract

This is a real functionality problem, not just a style mismatch: the exact race and crash boundaries Phase 8 is supposed to harden are still uncontrolled in the implementation.

Recommended fix: move the `dispatched` transition and the post-`run_async` update onto CAS writes, and make the dispatcher persist the full recovery handle needed by the poller before/after spawn according to the approved crash-boundary design.

### 3. Medium: Task 10’s K8s callback auth is not actually wired into runtime startup
The new `TokenReviewAuthMiddleware` exists in `src/agents/runtime/auth.py`, but no runtime or workflow app installs it. `get_auth_mode()` is only defined, never consumed. `KubernetesSpawner` also defaults `projected_sa_token=False`, and `_create_spawner()` in `src/agents/workflow/entrypoint.py` never enables it or reads any auth-mode env. On the sender side, `src/agents/runtime/callback.py` still authenticates callbacks with `AGENT_API_TOKEN`, not a projected service-account token.

So the commit message claims Phase 8 includes K8s TokenReview auth, but the deployed path is still shared-secret callback auth only. That is a security gap because the code does not yet enforce the per-pod identity boundary the task describes.

Recommended fix: plumb `AUTH_MODE` through app creation, install `TokenReviewAuthMiddleware` when selected, enable projected SA tokens for spawned K8s jobs in that mode, and teach the callback client to read and send the projected token instead of the shared secret.

### 4. Medium: the new Phase 8 E2E does not prove the multi-replica behavior it claims
`tests/e2e/test_phase8_multi_replica.py` is described as the integration capstone, but it never submits a workflow, never exercises callback completion, never verifies duplicate or stale callback handling, never simulates a lost callback, never checks poller-driven recovery, never kills a replica between persist and advance, and never creates an orphaned labeled resource for reconciliation. The "visibility labels" check explicitly passes even when there are no jobs: `'(none yet — labels verified in code)'`.

That means the tests can pass while all of the phase’s hardest runtime contracts remain broken. For this phase, that is a material quality issue because the broadest claims are specifically about cross-replica and failure-path behavior.

Recommended fix: either narrow the test’s stated scope to what it actually verifies, or add real workflow-submission scenarios that cover the callback, retry, recovery, and orphan-cleanup paths promised in `phase-8-tasks.md`.

## Perspective Check
- Functionality: not covered cleanly; async recovery and multi-replica dispatch safety are still incomplete.
- Quality: unit coverage is good for local helpers, but the committed E2E does not validate the phase’s headline behaviors.
- Security: not covered cleanly for Kubernetes; TokenReview mode is scaffolded but not active in the runtime path.

## Verification
- Reviewed scope from `docs/design/cloud-agents/phase-8-tasks.md`
- Inspected git context with:
  - `git status --short`
  - `git log -8 --stat --decorate --oneline`
  - `git diff --name-only c66952ad..HEAD`
- Read the main implementation and test files under `src/agents/runtime/`, `src/agents/workflow/`, `src/agents/spawner/`, and `tests/`
- Ran:
  - `uv run pytest tests/unit/agents/workflow/test_ingest.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_advance.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_executor_dual_mode.py tests/unit/agents/runtime/test_callback.py tests/unit/agents/runtime/test_token_review.py tests/unit/agents/spawner/test_kubernetes_spawner.py -q`
  - Result: `60 passed in 0.16s`
- Did not run `tests/e2e/test_phase8_multi_replica.py`; it provisions Kind/PostgreSQL images and no running cluster was pre-verified for this review pass.

## Summary
Phase 8 has substantial implementation progress, but I would not mark it review-clean yet. The biggest blockers are at the seams: the deployed poller is not wired to perform the recovery role the phase depends on, the async dispatch path does not yet enforce the documented CAS crash-boundary contract, and the K8s TokenReview auth path is still scaffolded rather than active.
