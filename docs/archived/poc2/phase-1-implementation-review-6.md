# Review: PoC2 Phase 1 review-round-5 fixes

Scope: verify uncommitted working-tree changes against the 3 findings from `phase-1-implementation-review-5.md`.

## Findings

### 1. HIGH — Worker never injects spawner: PASS

**What review 5 required:** The Temporal worker must bind a real spawner into the registered activity so the live worker path does not fall back to the stub.

**What was done:**

- `src/agents/workflow/temporal_worker.py` adds `_bind_sandbox_activity(spawner)` which creates a `@activity.defn(name="run_sandbox_step")` wrapper that calls `run_sandbox_step(input, spawner=spawner)`.
- `build_worker_config()` now accepts a `spawner: Optional[Any]` parameter. When non-None, it registers the bound activity; when None, it registers the raw function (stub mode).
- `src/agents/workflow/temporal_entrypoint.py` adds `_create_spawner()` which reads `WORKFLOW_SPAWNER` env var and creates a `KubernetesSpawner` or `PodmanSpawner` accordingly, or returns None for stub mode.
- `build_temporal_app()` calls `_create_spawner()` and passes the result to `build_worker_config(spawner=spawner)`.

**Verified:** Runtime confirmation that `build_worker_config(spawner=mock)` produces a bound activity distinct from the raw `run_sandbox_step`, and `build_worker_config()` (no spawner) uses the raw function. The injection chain from entrypoint through worker config to activity is complete.

### 2. HIGH — workflow_name resolution has no public population path: PASS

**What review 5 required:** Definition management routes must exist so callers can submit workflow definitions and then reference them by `workflow_name`.

**What was done:**

- `src/agents/workflow/temporal_api.py` conditionally registers three definition routes when `definition_store` is provided:
  - `POST /v1/workflows/definitions` — submit a definition, returns `{name, version}`
  - `GET /v1/workflows/definitions` — list all active definitions
  - `GET /v1/workflows/definitions/{name}` — get a definition by name, 404 if not found
- `src/agents/workflow/temporal_entrypoint.py` already passes `DefinitionStore()` to `build_temporal_router()`, so these routes are registered in the live app.

**Verified:** Runtime route introspection confirms the definition routes are present when a store is provided and absent when it is not. The `run_workflow` endpoint already has the `workflow_name` resolution logic that calls `definition_store.get()`, so the end-to-end path (submit definition, then run by name) is wired.

### 3. MEDIUM — Auth fails open at startup: PASS

**What review 5 required:** When `AUTH_REQUIRED=true` and auth fails to initialize, the entrypoint must fail closed (abort startup) rather than silently running with unauthenticated endpoints.

**What was done:**

- `src/agents/workflow/temporal_entrypoint.py` reads `AUTH_REQUIRED` env var (default `false`).
- `_get_auth_dependency()` catches exceptions from auth initialization. If `AUTH_REQUIRED` is true, it raises `RuntimeError("AUTH_REQUIRED=true but auth dependency failed to initialize. Refusing to start with unauthenticated workflow endpoints.")`. If false, it logs a warning and returns None (dev/test mode).

**Verified:** Runtime confirmation: with `AUTH_REQUIRED=true` and a broken auth import, `_get_auth_dependency()` raises `RuntimeError`. With `AUTH_REQUIRED` unset/false, it returns None with a warning log. The fail-closed behavior is correct.

## New Issues

### 1. LOW: No unit tests for the three fixes

None of the three fixes have dedicated unit tests:

- `test_worker.py` does not test `_bind_sandbox_activity` or the `spawner` parameter of `build_worker_config`.
- `test_entrypoint.py` does not test `_get_auth_dependency` fail-closed behavior or `_create_spawner`.
- `test_api.py` does not test the definition management routes.

The implementation is correct and was verified at runtime, but regression coverage is missing. For a PoC this is acceptable; for production these would need tests.

## Verification

- Inspected uncommitted diffs:
  - `git diff src/agents/workflow/temporal_worker.py`
  - `git diff src/agents/workflow/temporal_api.py`
  - `git diff src/agents/workflow/temporal_entrypoint.py`
- Runtime verification:
  - `build_worker_config(spawner=mock)` produces bound activity, `build_worker_config()` uses raw function
  - Route introspection confirms definition routes present with store, absent without
  - `_get_auth_dependency()` raises RuntimeError when AUTH_REQUIRED=true and auth fails; returns None otherwise
- `uv run ruff check src/agents/workflow/temporal_*.py tests/unit/agents/workflow/temporal/` -> All checks passed
- `uv run pytest tests/unit/agents/ -q` -> 370 passed, 0 failures
- `git status` -> 3 modified files (the fixes), no test file changes

## Summary

All 3 findings from review round 5 are resolved. The spawner is now injected through the worker config into the live activity path. Definition management routes provide a public population path for workflow_name resolution. Auth fails closed when AUTH_REQUIRED=true. One low-severity observation: none of the fixes have dedicated unit tests (acceptable for PoC scope).

**Verdict: PASS**
