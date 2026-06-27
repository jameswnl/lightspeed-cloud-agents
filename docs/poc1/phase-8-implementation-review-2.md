# Review: commit 4cdb26e69d540d16b5efeaeb9c0073f42747dec8

## Findings

### 1. High: recovered workflows still cannot dispatch the next async step
This commit wires `RecoveryPoller` with a `spawner` and `client_factory` in `src/agents/workflow/entrypoint.py`, which is an improvement, but it does not fix the advancement half of the problem. In `src/agents/workflow/advancement.py`, every successful recovery path still calls `advance_workflow(self._persistence, None, wf.workflow_id)`. `advance_workflow()` only dispatches agent steps when its `dispatcher` argument is present, so a poller-recovered completed step can be persisted but the workflow still cannot move on to the next async agent step.

That leaves the core Phase 8 fallback contract incomplete: after a missed callback or replica failover, another replica can recover the finished result but still cannot continue the workflow autonomously.

Recommended fix: give `RecoveryPoller` access to the same `StepDispatcher` that the executor uses and pass that dispatcher into `advance_workflow()` from the recovered-result paths.

### 2. High: `AUTH_MODE=sa_token` still does not protect the callback ingress endpoint
The follow-up commit installs `TokenReviewAuthMiddleware` in `src/agents/runtime/server.py`, but the callback endpoint is not served by the agent runtime. It lives in the workflow runner app at `POST /v1/workflows/{workflow_id}/steps/{step_name}/result` inside `src/agents/workflow/api.py`. That workflow app still always installs `BearerAuthMiddleware` from `AGENT_API_TOKEN` and never consults `AUTH_MODE`.

So the commit message claims TokenReview auth is now installed, but the actual callback ingress path remains shared-secret only. For the specific K8s trust-boundary finding from review 1, this means the important receiver-side binding is still not implemented.

Recommended fix: plumb auth-mode selection into `create_workflow_app()` as well, and install `TokenReviewAuthMiddleware` on the workflow runner when `AUTH_MODE=sa_token`.

## Perspective Check
- Functionality: still has a major gap; recovery can ingest results but not fully resume async progression.
- Quality: partially improved, but the new commit does not add tests covering the remaining recovery-dispatch seam or workflow-app auth-mode selection.
- Security: still has a major gap on Kubernetes; the callback receiver path is not using TokenReview auth.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -6`
  - `git show --stat --decorate --summary 4cdb26e69d540d16b5efeaeb9c0073f42747dec8`
  - `git diff --name-only 4cdb26e69d540d16b5efeaeb9c0073f42747dec8^ 4cdb26e69d540d16b5efeaeb9c0073f42747dec8`
- Read:
  - `src/agents/runtime/server.py`
  - `src/agents/workflow/entrypoint.py`
  - `src/agents/workflow/step_dispatcher.py`
  - `src/agents/workflow/api.py`
  - `src/agents/workflow/advancement.py`
  - `tests/e2e/test_phase8_multi_replica.py`
  - `tests/unit/agents/workflow/test_entrypoint.py`
  - `tests/unit/agents/runtime/test_server.py`
- Ran:
  - `uv run pytest tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/runtime/test_server.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/runtime/test_token_review.py -q`
  - Result: `42 passed`

## Summary
This commit is a partial improvement over the first Phase 8 implementation review: the poller is now better wired for polling and cleanup, and the E2E scope statement is more honest. But it does not fully resolve the earlier functionality and security findings, because recovered workflows still cannot dispatch onward and the workflow runner callback endpoint still does not honor `AUTH_MODE=sa_token`.
