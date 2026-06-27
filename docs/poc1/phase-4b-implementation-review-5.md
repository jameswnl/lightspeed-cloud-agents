# Review: Phase 4b Implementation (Commit `2836ad0d`)

## Findings

### 1. Major: PostgreSQL persistence is selectable, but startup still does not initialize the database backend

This commit usefully wires two previously-standalone capabilities into the runtime:

- `spawn: on-demand` is now part of `WorkflowStepSpec`
- `WorkflowExecutor` now accepts and uses an `AgentSpawner`
- the workflow entrypoint can now choose a persistence backend

However, the workflow runner startup path still does not call `PostgresPersistence.initialize()`.

#### Why this matters

This remains a real runtime bug:

- the entrypoint can now select `PostgresPersistence`
- but if the backing table does not already exist, normal workflow operations may fail
- the startup path gives the appearance of a fully-wired database persistence layer without actually preparing the database

That keeps the production-persistence path only partially complete.

#### Recommendation

When the workflow runner selects `PostgresPersistence`, initialize it during startup before serving requests.

## What Improved

This commit does close one of the two open integration gaps from the previous review:

- on-demand spawning is now connected to `WorkflowExecutor`

That is meaningful Phase 4b progress.

## Perspective Check

- Functionality: spawner integration improved; persistence wiring still incomplete because initialization is missing
- Quality: runtime integration is moving in the right direction, but the startup contract for the database backend is still incomplete
- Security: no new trust-boundary regression stood out in this commit; the remaining issue is startup correctness

## Verification

I re-ran the targeted tests for the touched integration areas:

```bash
uv run pytest tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_executor.py tests/unit/agents/workflow/test_postgres_persistence.py tests/unit/agents/spawner/test_base.py -q
```

Result:

- **34 passed**

## Summary

This is a good follow-up commit and it resolves the spawner integration gap.

The one remaining blocker for Phase 4b `LGTM` is:

- PostgreSQL persistence is still not fully initialized in the workflow runner startup path

So the watcher should continue.
