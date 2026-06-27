# Review: commit 7d344a63

## Findings

### 1. High: TokenReview auth still does not bind the callback caller to the specific spawned job/attempt
This commit correctly adds the missing `authentication.k8s.io/tokenreviews` RBAC, which fixes the immediate in-cluster permission failure from the prior review. But the current Task 10 contract is still not actually met.

The problem is twofold:

- `src/agents/runtime/auth.py` only checks `result.status.authenticated` from TokenReview and does not propagate or verify the caller identity against the target workflow step.
- `src/agents/spawner/kubernetes_spawner.py` still launches spawned agent pods with a single shared ServiceAccount, `workflow-runner`, via `service_account_name=self._service_account`, and `src/agents/workflow/entrypoint.py` still defaults `SPAWNER_SERVICE_ACCOUNT` to `workflow-runner`.

So after this commit, the workflow runner can verify that *some* valid token for the `cloud-agents` audience was presented, but it still cannot distinguish which spawned job made the callback, nor bind that callback to the specific job/attempt that the workflow state expects. Any pod using the same ServiceAccount could still POST a result for another workflow step.

That falls short of the Phase 8 Task 10 requirement that “the ingest endpoint binds caller identity to the specific spawned Job's ServiceAccount.” The RBAC fix is necessary, but it is not sufficient to satisfy the intended trust boundary.

Recommended fix: either generate per-job ServiceAccounts (or another per-attempt identity primitive) and compare TokenReview identity against the expected caller for that workflow step, or explicitly rescope the docs/tasks so Kubernetes callback mode remains “authenticated but not per-job bound” rather than production-ready Task 10.

## Perspective Check
- Functionality: the latest commit fixes the prior RBAC blocker.
- Quality: improved, with a manifest-level regression test for RBAC.
- Security: still not fully resolved; caller identity is authenticated, but not bound to the specific spawned job/attempt described by the Task 10 contract.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -6`
  - `git diff --name-only 325de12e..HEAD`
  - `git log --stat --decorate --oneline 325de12e..HEAD`
- Read:
  - `deploy/kind/rbac.yaml`
  - `src/agents/runtime/auth.py`
  - `src/agents/spawner/kubernetes_spawner.py`
  - `src/agents/workflow/entrypoint.py`
  - `tests/unit/agents/runtime/test_token_review.py`
  - `docs/design/cloud-agents/phase-8-tasks.md`
- Ran:
  - `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/unit/agents/runtime/test_callback.py tests/unit/agents/test_remote_agent_client.py tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_step_dispatcher.py -q`
  - Result: `49 passed`

## Summary
This commit fixes the specific RBAC issue from `phase-8-implementation-review-6.md`, but I would still not mark Phase 8 LGTM yet. The remaining blocker is the Task 10 trust-boundary requirement itself: the system now validates TokenReview successfully, but it still does not bind the callback caller to the specific spawned job/attempt.
