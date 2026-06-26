# Review: commit 325de12e

## Findings

### 1. High: TokenReview auth still lacks the RBAC needed to work in-cluster
This commit correctly updates `deploy/kind/workflow-runner.yaml` to mount a projected ServiceAccount token and set `AUTH_MODE=sa_token`, which closes the deployment realism gap from the prior review. But the cluster RBAC still does not grant permission to call the Kubernetes TokenReview API.

`src/agents/runtime/auth.py` implements `TokenReviewAuthMiddleware` by calling `AuthenticationV1Api.create_token_review(...)`. The Phase 8 task doc also explicitly requires `authentication.k8s.io/tokenreviews` create permission. However, `deploy/kind/rbac.yaml` only grants the workflow runner access to Jobs, Pods, and Services; it does not include any rule for `tokenreviews`.

That means the `sa_token` path is still not actually runnable in-cluster: the middleware will try to validate incoming tokens through the API server, but the workflow runner ServiceAccount does not have permission to perform that call.

Recommended fix: add an RBAC rule granting `create` on `authentication.k8s.io/tokenreviews` for the `workflow-runner` ServiceAccount, and add a manifest-level regression test if possible.

## Perspective Check
- Functionality: almost there, but the in-cluster TokenReview path is still blocked by missing RBAC.
- Quality: improved, with better manifest coverage, but one critical deployment requirement from the task doc is still missing.
- Security: improved, but not fully operational until the workflow runner is authorized to perform TokenReview validation.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -6`
  - `git diff --name-only 55834b27..HEAD`
  - `git log --stat --decorate --oneline 55834b27..HEAD`
- Read:
  - `deploy/kind/workflow-runner.yaml`
  - `deploy/kind/rbac.yaml`
  - `src/agents/runtime/auth.py`
  - `tests/unit/agents/runtime/test_token_review.py`
  - `docs/design/cloud-agents/phase-8-tasks.md`
- Ran:
  - `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/unit/agents/runtime/test_callback.py tests/unit/agents/test_remote_agent_client.py tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_step_dispatcher.py -q`
  - Result: `49 passed`

## Summary
This commit fixes the previously reported workflow-runner token-mount gap, but Phase 8 is still not ready for LGTM. The remaining blocker is Kubernetes RBAC: the workflow runner still cannot call `create tokenreviews`, so the new `sa_token` receiver path is not yet operational in-cluster.
