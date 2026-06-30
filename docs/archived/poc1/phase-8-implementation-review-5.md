# Review: commit 55834b27

## Findings

### 1. High: `sa_token` mode is still not deployable for the workflow runner
This commit unifies runner-side auth token lookup in code, but the actual workflow-runner deployment assets still do not mount the projected token file that `get_runner_auth_token()` now depends on. The new helper in `src/agents/runtime/auth.py` reads `/var/run/secrets/cloud-agents/token` when `AUTH_MODE=sa_token`, yet `deploy/kind/workflow-runner.yaml` does not set `AUTH_MODE`, does not add a projected token volume, and does not mount anything at that path.

That means the latest code fixes the helper-level call sites, but a real workflow-runner pod still has no credential to present on runner-to-agent calls in `sa_token` mode. In practice, `get_runner_auth_token()` will return `None`, and the protected runner-to-agent path will still fail once TokenReview auth is enabled on the receiver side.

Recommended fix: update the workflow-runner Kubernetes deployment/manifests to mount a projected ServiceAccount token for audience `cloud-agents` at `/var/run/secrets/cloud-agents/token`, and set `AUTH_MODE=sa_token` in the same deployment when exercising that mode.

### 2. Medium: tests still do not cover the deployment/runtime wiring for runner `sa_token`
The new tests cover `get_runner_auth_token()` as a pure helper and the updated call sites stay green, but nothing verifies that a workflow-runner deployment actually provides the projected token path the helper expects. That leaves the shipped Kubernetes wiring gap invisible to the test suite.

Recommended fix: add one deployment/entrypoint-level test or manifest validation check that asserts the runner pod mounts the projected token volume when `AUTH_MODE=sa_token` is intended.

## Perspective Check
- Functionality: improved at the code-callsite level, but still incomplete in real deployment because the runner pod is missing the projected token mount.
- Quality: remaining gap; helper tests exist, but the deployment wiring that makes the feature runnable is still unverified.
- Security: still incomplete for Kubernetes rollout; the intended per-pod TokenReview flow depends on a runner credential source that is not present in the committed runner manifest.

## Verification
- Inspected git context with:
  - `git status --short`
  - `git log --oneline --decorate -8`
  - `git diff --name-only 81b67abaafff74cc73971c2ba001a5c6a296a94f..HEAD`
  - `git log --stat --decorate --oneline 81b67abaafff74cc73971c2ba001a5c6a296a94f..HEAD`
- Read:
  - `src/agents/runtime/auth.py`
  - `src/agents/workflow/entrypoint.py`
  - `src/agents/workflow/executor.py`
  - `src/agents/workflow/step_dispatcher.py`
  - `deploy/kind/workflow-runner.yaml`
  - `tests/unit/agents/runtime/test_token_review.py`
- Ran:
  - `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/unit/agents/runtime/test_callback.py tests/unit/agents/test_remote_agent_client.py tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_step_dispatcher.py -q`
  - Result: `48 passed`

## Summary
This commit closes the prior code-level runner auth inconsistencies, but Phase 8 is still not LGTM. The remaining blocker is deployment realism: the workflow-runner manifest does not yet provide the projected ServiceAccount token that the new runner-side `sa_token` helper requires.
