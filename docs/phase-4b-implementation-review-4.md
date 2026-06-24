# Review: Phase 4b Implementation (Commit `2836ad0d`)

## Findings

### 1. High: spawned agents are cleaned up using `step.agent` instead of the spawned endpoint/handle

The executor now has on-demand spawn wiring, but the cleanup path passes `step.agent` to `destroy()` instead of the `spawned_endpoint` returned by the spawner.

#### Why this matters

This is a direct **functionality** issue:

- `spawn()` returns a runtime-specific handle/endpoint
- cleanup should destroy that exact spawned instance
- the current code instead passes the static agent name

That means the finally-block cleanup contract is wrong, and in the real spawner implementations this can cause:

- leaked spawned agents
- cleanup failures
- cleanup operating on the wrong target

#### Recommendation

Pass `spawned_endpoint` (or the actual spawn handle type) into `destroy()`, not `step.agent`.

### 2. High: PostgreSQL persistence is now selected by entrypoint, but `initialize()` is never called

The workflow entrypoint can now construct `PostgresPersistence`, which is good progress. But the persistence backend requires `initialize()` to create tables, and the startup path never calls it.

#### Why this matters

This is a real **functionality** and **quality** bug:

- the feature now looks wired
- the first real save/load path can still fail at runtime if tables do not exist
- the current unit tests do not prove the startup path initializes the database backend

So this is not just incomplete polish; it is a likely runtime failure in the intended deployment path.

#### Recommendation

When the workflow runner selects `PostgresPersistence`, call `await initialize()` during startup before creating or serving the app.

## What Improved

This commit does move Phase 4b forward in two meaningful ways:

- workflow step schema now supports `spawn: on-demand`
- workflow runner can now choose a PostgreSQL persistence backend in principle

Those are real integration steps beyond the previous foundation-only state.

## Perspective Check

- Functionality: remaining runtime bugs in spawned-agent cleanup and database initialization
- Quality: integration is improving, but these are still untested startup/cleanup path defects
- Security: no new trust-boundary regression stood out in this slice; the main issues are correctness of the new runtime plumbing

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_executor.py tests/unit/agents/workflow/test_postgres_persistence.py tests/unit/agents/spawner/test_base.py -q
```

Result:

- **31 passed**

The existing tests pass, but they do not catch the two integration issues above.

## Summary

This commit is valuable progress, but it introduces or leaves visible two concrete runtime integration bugs:

1. spawned-agent cleanup uses the wrong argument
2. PostgreSQL persistence is selected without table initialization

So Phase 4b implementation is still **not** at `LGTM` yet.
