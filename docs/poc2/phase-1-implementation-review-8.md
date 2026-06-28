# Review: PoC2 Phase 1 review-round-7 fix verification

Scope: verify the fix for the route-shadowing finding from `phase-1-implementation-review-7.md` --
`GET /v1/workflows/definitions` was being swallowed by `GET /v1/workflows/{workflow_id}`.

## Verdict: PASS

The route-ordering bug is fixed and covered by a targeted regression test.

## Findings

No new findings.

## Verification

### 1. Route ordering in `src/agents/workflow/temporal_api.py`

Inside `build_temporal_router()`, routes are now registered in this order:

1. `POST /run` (line 68)
2. `POST /definitions` (line 125, inside `if definition_store:`)
3. `GET /definitions` (line 133)
4. `GET /definitions/{name}` (line 139)
5. `POST /{workflow_id}/approve` (line 151)
6. `GET /{workflow_id}` (line 163)
7. `GET /{workflow_id}/events` (line 172)
8. `POST /{workflow_id}/cancel` (line 207)

The literal `/definitions` and `/definitions/{name}` routes are registered before the
parameterized `/{workflow_id}` routes. FastAPI evaluates routes in registration order,
so a request to `/v1/workflows/definitions` now matches `list_definitions()` rather
than `get_workflow_status(workflow_id="definitions")`. This is the correct fix.

### 2. Regression test in `tests/unit/agents/workflow/temporal/test_api.py`

`TestDefinitionRoutes::test_get_definitions_returns_list` (line 153) does exactly what
review 7 recommended:
- Constructs a router with a real `DefinitionStore` so the definition routes are active.
- Issues `GET /v1/workflows/definitions`.
- Asserts `status_code == 200`.
- Asserts `isinstance(response.json(), list)` -- a workflow-status response would be a
  dict with `{"steps": {}, "events": []}`, so this assertion distinguishes the two handlers.

### 3. Test run

```
uv run pytest tests/unit/agents/workflow/temporal/test_api.py -v
  8 passed in 0.18s
```

All 8 tests pass, including `test_get_definitions_returns_list`.

### 4. Linter

```
uv run ruff check src/agents/workflow/temporal_api.py
  All checks passed!
```

## Summary

The single finding from review 7 is resolved. Definition routes are registered before
the generic `{workflow_id}` routes, eliminating the shadowing, and a regression test
guards against future reordering. No new issues found in this round.
