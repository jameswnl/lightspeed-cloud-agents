# Review: PoC2 Phase 1 review-round-3 fixes

Scope: verify commit `28eef3d2` against the findings from `phase-1-implementation-review-3.md`.

## Findings

### 1. High: the live worker still never injects a spawner, so the runtime path still falls back to the stub activity
`src/agents/workflow/temporal_activities.py` still has this branch:

- if `spawner is None`, log and return `{"status": "completed", "output": {"summary": ...}}`

But `src/agents/workflow/temporal_worker.py` still registers the bare activity function:

- `activities=[run_sandbox_step, build_escalation_activity]`

There is still no worker-side injection of a real spawner instance, no activity context wrapper, and no alternative registration that binds `spawner`. In the actual Temporal worker process, `run_sandbox_step()` therefore still executes with its default `spawner=None` and returns the stub result instead of spawning a sandbox pod.

This means the most important runtime seam is still not implemented in the deployed path, even though helper-level tests now pass. The code now supports a real spawner in direct unit tests, but the live worker still cannot reach that branch.

Recommended fix: inject a real spawner when registering activities, or wrap `run_sandbox_step` in a bound activity function that supplies the configured spawner instance. Add a worker/entrypoint-level test that proves the registered activity path uses a non-`None` spawner in production mode.

### 2. High: `workflow_name` resolution is still effectively unusable in the real app
`src/agents/workflow/temporal_entrypoint.py` now passes `DefinitionStore()` into `build_temporal_router()`, but that store is:

- process-local and in-memory
- not connected to shared persistence
- not populated from any API in `src/agents/workflow/temporal_api.py`

The Temporal router still only exposes `/run`, `/approve`, `/{id}`, `/{id}/events`, and `/cancel`. There are no definition submission/list/get/delete routes in the Temporal API, and the entrypoint does not preload definitions into the new store. So while the code no longer errors with "definition store missing", a real caller still has no public way to make `workflow_name` resolution succeed.

This is a caller-visible contract gap: the helper seam is wired, but the public behavior the fix claims to restore is still unavailable.

Recommended fix: either restore Temporal definition-management routes or preload the definition store from a persistent/shared source during startup. Then add an API test that starts from the public contract and proves a `workflow_name` request can actually succeed.

### 3. Medium: auth still fails open at startup
`src/agents/workflow/temporal_entrypoint.py` now calls `_get_auth_dependency()`, but that helper catches any exception and returns `None`, logging only a warning:

- auth import/config failure -> app still starts
- router is built with `auth_dependency=None`
- all workflow control endpoints become unauthenticated

That is a trust-boundary problem in the real app, because a configuration or import error silently degrades the service into an open control plane instead of failing startup.

Recommended fix: fail closed for non-dev configurations. If auth is expected and cannot be initialized, abort startup rather than exposing unauthenticated workflow control endpoints.

## Perspective Check
- Functionality: remaining gaps. The fix commit improves helper wiring, but the live worker still uses the stub activity path and `workflow_name` still lacks a usable public population path.
- Quality: remaining gaps. The current tests validate helpers and direct activity calls, but they still do not prove the actual worker/entrypoint runtime seam.
- Security: remaining gap. Startup still degrades to unauthenticated endpoints when auth initialization fails.

## Verification
- Inspected git context with:
  - `git log --oneline --decorate 97f7c4ba25540e87937e40eb4187b26959541ae9..HEAD`
  - `git diff --name-only 97f7c4ba25540e87937e40eb4187b26959541ae9..HEAD`
  - `git show --stat --summary --decorate 28eef3d27f04892f1da29a982d099f64d54a95e1`
- Read the changed implementation and tests together:
  - `src/agents/workflow/temporal_activities.py`
  - `src/agents/workflow/temporal_entrypoint.py`
  - `src/agents/workflow/temporal_workflow.py`
  - `src/agents/workflow/temporal_api.py`
  - `src/agents/workflow/temporal_worker.py`
  - `tests/unit/agents/workflow/temporal/test_activities.py`
  - `tests/unit/agents/workflow/temporal/test_entrypoint.py`
- Searched the workflow package for definition-store usage and public definition-management routes.
- Ran:
  - `uv run pytest tests/unit/agents/workflow/temporal -q` -> `56 passed`
  - `uv run pytest tests/unit/agents/ -q` -> `370 passed`
  - `uv run ruff check src/agents/workflow/temporal_*.py tests/unit/agents/workflow/temporal` -> passed

## Summary
This commit fixes several helper-level seams from the previous review, and the targeted tests/lint checks pass. But the real runtime path is still not review-clean: the Temporal worker still does not inject a spawner, `workflow_name` still lacks a usable public population path, and auth still fails open at startup. So this review-fixes round is not resolved yet.
