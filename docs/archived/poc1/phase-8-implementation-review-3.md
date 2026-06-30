# Review: commit range 4cdb26e69d540d16b5efeaeb9c0073f42747dec8..625e8d43

## Findings

### 1. High: protected async execution is still broken, and `sa_token` mode now makes that failure unavoidable
This commit correctly wires `RecoveryPoller` with a dispatcher and switches the workflow runner app to `TokenReviewAuthMiddleware` in `src/agents/workflow/api.py`, but the sender-side auth path still does not match what the receivers now require.

There are two parts to the problem:

- `src/agents/runtime/callback.py` still authenticates callback POSTs with `AGENT_API_TOKEN`, not a projected ServiceAccount token. In `AUTH_MODE=sa_token`, the workflow runner now expects a TokenReview-valid bearer token, so these callbacks will be rejected.
- `src/agents/remote_agent_client.py` never attaches `self._auth_token` in `run_async()` or `poll_run()`. That means the async submit/poll path used by `StepDispatcher.dispatch_async()` and the recovery poller does not send any bearer token at all, even though the spawned agent runtime protects `/v1/run` and `/v1/runs/{id}`. In other words, the protected async path is currently broken even under shared-secret auth, and `sa_token` mode only makes that mismatch more explicit.

So while the latest commit fixes the prior dispatcher wiring issue, the recovered workflow still cannot actually complete a protected async round-trip in production: the runner cannot authenticate async submit/poll requests to the spawned agent, and the spawned agent cannot authenticate callback results back to the workflow runner in `sa_token` mode.

Recommended fix: make `RemoteAgentClient.run_async()` and `poll_run()` send the configured bearer token just like `run()` does, then plumb a real ServiceAccount-token credential source for `AUTH_MODE=sa_token` on both the runner-to-agent and agent-to-runner paths instead of continuing to rely on `AGENT_API_TOKEN`.

### 2. Medium: the current tests still miss the protected async auth seam entirely
The passing suites did not catch the bug above because they do not exercise authenticated async traffic:

- `tests/unit/agents/test_remote_agent_client.py` covers auth headers for `run()`, but not for `run_async()` or `poll_run()`
- the workflow API and entrypoint tests do not cover `AUTH_MODE=sa_token` on the workflow runner together with an actual async dispatch/callback exchange

This leaves a high-risk path unverified while the local suites stay green.

Recommended fix: add focused tests that assert auth headers are sent on async submit/poll, plus one integration-style test that covers a protected async callback flow under the selected auth mode.

## Perspective Check
- Functionality: improved over the previous review; the dispatcher wiring gap is fixed, but protected async submit/poll/callback still does not work.
- Quality: remaining gap; test coverage still does not exercise the authenticated async path that phase 8 depends on.
- Security: remaining gap; `sa_token` receiver-side checks are now installed, but the caller-side credentials still do not match that trust boundary.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -8`
  - `git diff --name-only 4cdb26e69d540d16b5efeaeb9c0073f42747dec8..HEAD`
  - `git log --stat --decorate --oneline 4cdb26e69d540d16b5efeaeb9c0073f42747dec8..HEAD`
  - `git diff 4cdb26e69d540d16b5efeaeb9c0073f42747dec8..625e8d43 -- src/agents/workflow/advancement.py src/agents/workflow/api.py src/agents/workflow/entrypoint.py`
- Read:
  - `src/agents/workflow/advancement.py`
  - `src/agents/workflow/api.py`
  - `src/agents/workflow/entrypoint.py`
  - `src/agents/runtime/auth.py`
  - `src/agents/runtime/callback.py`
  - `src/agents/remote_agent_client.py`
  - `tests/unit/agents/workflow/test_api.py`
  - `tests/unit/agents/workflow/test_advancement.py`
  - `tests/unit/agents/workflow/test_entrypoint.py`
  - `tests/unit/agents/runtime/test_server.py`
  - `tests/unit/agents/test_remote_agent_client.py`
- Ran:
  - `uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/runtime/test_server.py -q`
  - `uv run pytest tests/unit/agents/test_remote_agent_client.py -q`
  - Results: `48 passed`, `13 passed`

## Summary
This commit resolves the two specific findings from `phase-8-implementation-review-2.md`, but Phase 8 is still not ready for LGTM. The remaining blocker is the authenticated async path itself: async submit/poll/callback still does not send credentials compatible with the protection now installed on the receiver side.
