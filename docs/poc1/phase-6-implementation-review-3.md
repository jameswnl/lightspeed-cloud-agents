# Review: Phase 6 follow-up range (`10f401d3..c79033a3`)

## Findings

### 1. High: submitted definitions are still stored per-process, so the runner is not stateless across replicas
The recent fixes addressed snapshot persistence and PostgreSQL CAS, but the definition catalog is still created as a fresh in-memory `DefinitionStore()` inside `create_workflow_app()`. Definitions submitted to one replica remain invisible to other replicas, so the main stateless multi-replica Phase 6 contract is still not met.

Why it matters:
- `POST /v1/workflows/definitions` still creates pod-local state
- `GET`, `DELETE`, and `POST /v1/workflows/run` by name can still disagree based on which replica receives the request
- the top-level production goal for Phase 6 is still incomplete even though workflow-run state is now more durable

Recommended fix:
- move `DefinitionStore` to shared persistence
- use that shared store for all definition CRUD and run-by-name lookups

### 2. High: `GET /v1/workflows/{id}` can still use the wrong definition for submitted workflows
The `approve` path now reconstructs an executor from `definition_snapshot`, which fixes the prior resume bug. But `GET /v1/workflows/{id}` still calls `app.state.executor.get_state()`, and `WorkflowExecutor.get_state()` runs `_check_approval_timeout()` against `self._definition.spec.steps`. For workflows started from a submitted definition, `self._definition` is still the default startup definition or stateless placeholder, not the persisted snapshot.

Why it matters:
- approval-timeout enforcement can still consult the wrong step definition for submitted workflows
- that leaves a real seam failure in a public follow-up operation even after the snapshot fix
- a workflow can behave differently depending on whether it is resumed or merely polled

Recommended fix:
- make `get_state()` reconstruct from `definition_snapshot` the same way `approve` now does, or move approval-timeout checks to logic that reads the persisted snapshot directly

### 3. Medium: the new snapshot and PostgreSQL CAS behavior still lacks focused regression tests
The recent fixes changed public behavior in `api.py`, `state.py`, and `postgres_persistence.py`, but the workflow tests still do not cover:
- polling a submitted workflow and hitting approval-timeout logic with its persisted snapshot
- PostgreSQL `save_cas()` success and stale-write rejection
- stateless multi-definition behavior across a shared backend

Why it matters:
- the most recent bug fix only covered one follow-up path (`approve`) and left another (`get_state`) exposed
- the PostgreSQL CAS implementation was added without a test proving the version predicate behavior it now claims

Recommended fix:
- add API tests for poll/resume on workflows started via `workflow_name`
- add PostgreSQL persistence tests for `save_cas()` success and version mismatch failure

## Perspective Check
- Functionality: remaining gaps. Resume correctness improved, but polling/state checks for submitted workflows still do not consistently use the persisted definition.
- Quality: remaining gaps. The recent fixes are only partially covered, and the shared-catalog behavior remains unverified.
- Security: no new security regression found in this follow-up range, but the broader callback-auth trust boundary from Phase 6 is still not implemented and remains out of LGTM scope.

## Verification
- Reviewed the current Phase 6 implementation at `HEAD` and compared it against prior findings
- Read the updated implementation in `src/agents/workflow/api.py`, `state.py`, `postgres_persistence.py`, `advancement.py`, `definition_store.py`, `persistence.py`, and `entrypoint.py`
- Re-checked `WorkflowExecutor.get_state()` and approval-timeout logic in `src/agents/workflow/executor.py`
- Searched workflow tests for coverage of `definition_snapshot`, `workflow_name`, stateless startup, and PostgreSQL CAS
- Ran `uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_postgres_persistence.py -q` -> `20 passed`

## Summary
Not LGTM yet. The latest fixes resolved the earlier approve/resume-definition issue and added a real PostgreSQL CAS path, but the definition catalog is still replica-local and polling a submitted workflow can still use the wrong definition for approval-timeout enforcement.
