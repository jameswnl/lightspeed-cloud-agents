# Review: PoC2 Phase 1 review-round-5 fixes

Scope: verify commit `91455555` against the findings from `phase-1-implementation-review-5.md`.

## Findings

### 1. Medium: `GET /v1/workflows/definitions` is shadowed by `GET /v1/workflows/{workflow_id}`
`src/agents/workflow/temporal_api.py` adds the definition-management routes after the generic `@router.get("/{workflow_id}")` route. In FastAPI, route order matters for overlapping patterns, so `GET /v1/workflows/definitions` is currently handled by `get_workflow_status(workflow_id="definitions")` instead of the intended definitions list handler.

I verified this with a `TestClient` smoke check against the router: `GET /v1/workflows/definitions` returns `200 {"steps":{},"events":[]}` rather than a list of stored definitions. By contrast, `POST /v1/workflows/definitions` and `POST /v1/workflows/run` with `workflow_name` do work, so the earlier high-severity population-path finding is substantially addressed. But the public definitions API is still behaviorally inconsistent because one of its advertised routes is swallowed by the generic workflow-status route.

Recommended fix: move the definitions routes above `/{workflow_id}` or otherwise make the path matching unambiguous, then add an API test that asserts `GET /v1/workflows/definitions` returns a definitions list rather than workflow status.

## Perspective Check
- Functionality: remaining gap. The spawner injection and run-by-name path are now wired, but the definitions list route is still broken due to route ordering.
- Quality: mixed. The targeted checks pass and the major prior seams are improved, but the current tests still missed a caller-visible API regression in the new definitions routes.
- Security: no new major issues found in this round. The `AUTH_REQUIRED=true` fail-closed behavior appears correctly implemented.

## Verification
- Inspected git context with:
  - `git log --oneline --decorate 28eef3d27f04892f1da29a982d099f64d54a95e1..HEAD`
  - `git diff --name-only 28eef3d27f04892f1da29a982d099f64d54a95e1..HEAD`
  - `git show --stat --summary --decorate 9145555536c112fc77b394f618b14cb66d20c729`
- Read the changed implementation in:
  - `src/agents/workflow/temporal_api.py`
  - `src/agents/workflow/temporal_entrypoint.py`
  - `src/agents/workflow/temporal_worker.py`
- Read the matching tests:
  - `tests/unit/agents/workflow/temporal/test_api.py`
  - `tests/unit/agents/workflow/temporal/test_worker.py`
  - `tests/unit/agents/workflow/temporal/test_entrypoint.py`
- Ran:
  - `uv run pytest tests/unit/agents/workflow/temporal -q` -> `56 passed`
  - `uv run pytest tests/unit/agents/ -q` -> `370 passed`
  - `uv run ruff check src/agents/workflow/temporal_*.py tests/unit/agents/workflow/temporal` -> passed
- Smoke-checked the live router behavior with `TestClient`:
  - `GET /v1/workflows/definitions` -> `200 {"steps":{},"events":[]}` (incorrect route match)
  - `GET /v1/workflows/definitions/foo` -> `404 {"detail":"Definition 'foo' not found"}`
  - `POST /v1/workflows/definitions` followed by `POST /v1/workflows/run` with `workflow_name` -> succeeded
- Confirmed worker-side spawner binding exists:
  - `build_worker_config(spawner=mock)` registers `bound_run_sandbox_step`
  - `build_worker_config()` registers raw `run_sandbox_step`

## Summary
This round resolves the main runtime seam concerns from review 5: the worker now has a spawner injection path, `workflow_name` runs can be populated through public submission, and auth fail-closed behavior is present. But the new definitions API still has a caller-visible route-order bug, so this fixes-review round is not clean yet.
