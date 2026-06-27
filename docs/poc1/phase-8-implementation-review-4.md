# Review: commit 81b67abaafff74cc73971c2ba001a5c6a296a94f

## Findings

### 1. High: recovery polling of protected agent runs is still broken
This commit updates `RemoteAgentClient.run_async()` and `poll_run()` to send `Authorization` when `auth_token` is configured, but the `RecoveryPoller` still does not configure one. In `src/agents/workflow/entrypoint.py`, the poller is constructed with `client_factory=lambda ep: RemoteAgentClient(ep)`, so recovered polling requests still hit `/v1/runs/{run_id}` with no bearer token at all.

That means the fallback path Phase 8 depends on is still broken when agent-runtime auth is enabled: the initial async submission may succeed, but the recovery poller cannot authenticate to fetch the result after a lost callback.

Recommended fix: wire the poller’s `client_factory` to pass the same auth credential source the normal runner-to-agent path uses, and add a regression test that covers authenticated `poll_run()` through `RecoveryPoller`.

### 2. High: `sa_token` mode still does not work for runner-to-agent calls
The callback side now reads `/var/run/secrets/cloud-agents/token` in `src/agents/runtime/callback.py`, but the runner-to-agent path still builds clients with `auth_token=get_api_token() or None` in both `src/agents/workflow/step_dispatcher.py` and `src/agents/workflow/executor.py`. `get_api_token()` still only reads `AGENT_API_TOKEN`; it does not read a projected ServiceAccount token.

So the commit message’s claim that the protected async round-trip now works under `sa_token` mode is still not true. In that mode, the agent runtime receiver expects a TokenReview-valid bearer token, but the workflow runner continues to send either a shared secret or nothing.

Recommended fix: introduce a runner-side auth-token helper analogous to callback `_get_auth_token()` and use it everywhere the workflow runner constructs `RemoteAgentClient` instances for spawned agent pods.

### 3. Medium: the new tests still miss both remaining seams
The added tests in `tests/unit/agents/test_remote_agent_client.py` and `tests/unit/agents/runtime/test_callback.py` are useful, but they do not cover:

- `RecoveryPoller` using an authenticated `client_factory`
- workflow-runner client construction under `AUTH_MODE=sa_token`

That leaves both remaining production bugs invisible to the local suites.

Recommended fix: add one test around `build_workflow_app()`/`RecoveryPoller` client creation and one around the runner-side auth token selection for spawned-agent calls.

## Perspective Check
- Functionality: improved, but still incomplete; the recovery poller cannot authenticate protected run polling.
- Quality: remaining gaps; tests cover the leaf helpers but not the integration points that actually assemble the auth path.
- Security: remaining gap; `sa_token` is still not applied consistently on the runner-to-agent side.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -6`
  - `git show --stat --decorate --summary $(git rev-parse HEAD)`
  - `git diff --name-only $(git rev-parse HEAD)^ $(git rev-parse HEAD)`
- Read:
  - `src/agents/remote_agent_client.py`
  - `src/agents/runtime/callback.py`
  - `src/agents/runtime/auth.py`
  - `src/agents/workflow/entrypoint.py`
  - `src/agents/workflow/step_dispatcher.py`
  - `src/agents/workflow/executor.py`
  - `tests/unit/agents/runtime/test_callback.py`
  - `tests/unit/agents/test_remote_agent_client.py`
- Ran:
  - `uv run pytest tests/unit/agents/runtime/test_callback.py tests/unit/agents/test_remote_agent_client.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_advancement.py -q`
  - Result: `41 passed`

## Summary
This commit fixes part of the async auth problem: helper-level async submit/poll now send auth headers, and callback auth selection is better. But Phase 8 still is not ready for LGTM because the assembled runtime flow is still inconsistent: the recovery poller is unauthenticated, and the workflow runner still has no `sa_token` credential source for runner-to-agent calls.
