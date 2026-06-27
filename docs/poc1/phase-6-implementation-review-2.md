# Review: Phase 6 follow-up commit (`af627597`)

## Findings

### 1. High: run-by-name workflows cannot be resumed correctly because follow-up requests use the wrong definition
`POST /v1/workflows/run` now creates a temporary `WorkflowExecutor` from the submitted definition when `workflow_name` is provided, but later operations still go through `app.state.executor`, which is built from the startup/default definition (or the stateless placeholder). `WorkflowState` still stores only `workflow_name`, not a definition snapshot or version, so approval resume and approval-timeout checks for submitted workflows will consult the wrong step list.

Why it matters:
- a paused workflow started from a submitted definition can resume against the wrong workflow shape
- `_check_approval_timeout()` and `resume()` both index into `self._definition.spec.steps`, so this is a real runtime seam failure rather than a bookkeeping issue
- this means the new run-by-name path is not safe for any workflow that needs pause/resume behavior

Recommended fix:
- persist `definition_version` and an immutable definition snapshot in `WorkflowState`
- route follow-up operations through an executor reconstructed from that persisted snapshot instead of the default app executor

### 2. High: the definition catalog is still process-local, so the runner is not stateless across replicas
The commit added run-by-name dispatch, but the definition API still uses a fresh in-memory `DefinitionStore()` inside `create_workflow_app()`. Definitions submitted to one replica remain invisible to other replicas, so the runner still does not satisfy the shared multi-replica catalog contract.

Why it matters:
- `POST /v1/workflows/definitions` is still replica-local state
- `GET`, `DELETE`, and `POST /v1/workflows/run` by name will disagree depending on which pod the load balancer hits
- this leaves the headline stateless multi-replica behavior unresolved

Recommended fix:
- back `DefinitionStore` with shared persistence
- ensure all definition CRUD and run-by-name lookups use that shared store

### 3. High: the claimed atomic CAS path still does not exist for PostgreSQL
`save_with_version()` now prefers `save_cas()` when available, but only `InMemoryPersistence` implements it. `PostgresPersistence` still does a blind upsert with no version predicate, so the production backend remains vulnerable to the same race the previous review called out.

Why it matters:
- the commit message claims `atomic CAS`, but the PostgreSQL path is unchanged
- multi-replica advancement on the real persistence backend can still race
- the new code only makes the in-memory test backend look correct

Recommended fix:
- add `save_cas()` to the persistence interface itself
- implement it in `PostgresPersistence` as a single compare-and-swap update guarded by `version`

### 4. Medium: the new run-by-name and stateless-startup behavior landed without matching regression tests
This commit adds new public behavior in `api.py` and `entrypoint.py`, but there are no new tests covering:
- run-by-name success/failure paths
- resume behavior for a workflow started from a submitted definition
- stateless startup without `workflow.yaml`
- CAS behavior on the PostgreSQL backend

Why it matters:
- the most important new seam in this commit is currently untested
- the existing passing tests mostly prove older/default-executor behavior, not the new contract

Recommended fix:
- add API tests for `workflow_name` dispatch and approval resume
- add startup tests for the stateless entrypoint path
- add PostgreSQL persistence tests for stale-write rejection

## Perspective Check
- Functionality: remaining gaps. The commit improves the API surface, but the run-by-name execution path still breaks on follow-up operations.
- Quality: remaining gaps. The key new behaviors were added without focused regression tests.
- Security: no new security regression found in this commit itself, but the broader callback-auth trust boundary from Phase 6 is still not implemented.

## Verification
- Reviewed latest commit `af627597` and its changed files
- Read the updated implementation in `src/agents/workflow/api.py`, `entrypoint.py`, `persistence.py`, and `advancement.py`
- Re-checked `src/agents/workflow/definition_store.py`, `postgres_persistence.py`, and `state.py` to verify whether the prior findings were fully addressed
- Searched workflow unit tests for coverage of `workflow_name`, stateless startup, and CAS behavior
- Ran `uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_advancement.py -q` -> `13 passed`

## Summary
Not LGTM yet. This follow-up commit improves the runner startup shape and adds a first pass at run-by-name execution, but it does not fully resolve the earlier review: submitted definitions are still replica-local, follow-up workflow operations still lose the selected definition context, and atomic CAS is still missing from the PostgreSQL backend.
