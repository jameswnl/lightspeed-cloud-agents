# Review: Fixes for Phase 1 Implementation Review Findings 2, 3, 5

Reviewer: independent code reviewer (automated)
Scope: Verifying fixes for findings 2, 3, and 5 from `phase-1-implementation-review-1.md`, plus old code deletion.

---

## Finding 5 (lint): PASS

**Verification command:**
```
uv run ruff check src/agents/workflow/temporal_activities.py \
  src/agents/workflow/temporal_api.py src/agents/workflow/temporal_context.py \
  src/agents/workflow/temporal_entrypoint.py src/agents/workflow/temporal_models.py \
  src/agents/workflow/temporal_worker.py src/agents/workflow/temporal_workflow.py \
  tests/unit/agents/workflow/temporal/
```

**Result:** `All checks passed!` -- 0 errors. The original review found 26 ruff issues; all are resolved.

---

## Finding 2 (auth): PASS

**Code review of `src/agents/workflow/temporal_api.py`:**
- `build_temporal_router` accepts `auth_dependency: Optional[Any] = None` (line 45).
- When provided, it is wrapped in `Depends()` and applied as a router-level dependency (line 57-58), which covers all four endpoints (run, approve, get-status, cancel).
- When `None`, the router has no dependencies, preserving backward compatibility for tests.

**Test review of `tests/unit/agents/workflow/temporal/test_api.py`:**
- `TestAuthEnforcement.test_unauthenticated_request_rejected` (line 153-176) creates a dependency that raises `HTTPException(401)`, passes it to `build_temporal_router`, and verifies all four endpoints return 401. This is a thorough test -- it covers every route, not just one.

No issues found.

---

## Finding 3 (conditions/parallel): PASS

**Code review of `src/agents/workflow/temporal_workflow.py`:**

1. **Uses `conditions.py` evaluator:** `_evaluate_condition` (line 188-210) bridges Temporal's `StepResult` to legacy `WorkflowState`, then calls `evaluate_condition()` from `conditions.py`. It does not use custom regex. The status mapping (`denied -> failed`, `escalated -> failed`) correctly translates Temporal-specific statuses into the legacy model's Literal union.

2. **Fails closed:** The `except ValueError: return False` block (line 209-210) catches the `ValueError` that `conditions.py` raises for unparseable conditions. Verified empirically that `conditions.py` does raise `ValueError` on invalid input (e.g., "this is not a valid condition expression"). This reverses the original fail-open behavior.

3. **Parallel group support:** The step loop (lines 63-81) checks `step.get("parallel_group")`, collects consecutive steps with the same group tag, and runs them via `asyncio.gather`. If any result has status "failed" or "denied", the workflow breaks. Sequential steps still advance with `i += 1`.

**Test review of `tests/unit/agents/workflow/temporal/test_workflow.py`:**
- `TestConditionFailClosed.test_invalid_condition_skips_step` (line 111-139): uses an unparseable condition string, verifies step2 gets `status="skipped"`. Directly proves fail-closed behavior.
- `TestParallelGroup.test_parallel_steps_run` (line 142-171): two steps with `parallel_group: "diag"`, verifies both complete. Proves the parallel path works.
- `TestConditionEvaluation.test_false_condition_skips_step` (line 80-108): step2's condition references `steps.r1.output.needs_fix == true`, which is false (stub returns `{"summary": "executed-step1"}`), so step2 is skipped. Proves condition evaluation integration with `conditions.py`.

All three sub-findings verified.

---

## Old code deletion: PASS (with one new issue)

**Deleted files verified absent (all 16):**
`executor.py`, `step_dispatcher.py`, `advancement.py`, `persistence.py`, `postgres_persistence.py`, `graph_executor.py`, `graph_state.py`, `graph_steps.py`, `graph_builder_factory.py`, `parallel.py`, `events.py`, `executor_protocol.py`, `retry.py`, `api.py`, `entrypoint.py`, `runtime/callback.py` -- all confirmed deleted.

**Kept modules verified present (all 10):**
`conditions.py`, `interpolation.py`, `auto_approve.py`, `advisory.py`, `permissions.py`, `escalation.py`, `notifier.py`, `definition.py`, `state.py`, `definition_store.py` -- all confirmed present.

**Dangling imports check:**
`uv run pytest tests/unit/agents/ -q --ignore=tests/unit/agents/workflow/temporal/test_activities.py` -- 361 passed, 0 failures. No dangling imports from deleted modules.

**New issue found:** `tests/unit/agents/workflow/temporal/test_activities.py` fails to collect:
```
ImportError: cannot import name 'compute_pod_name' from 'agents.workflow.temporal_activities'
```
The test file imports `compute_pod_name` and passes a `spawner` keyword argument to `run_sandbox_step` -- neither exists in the current stub implementation. This means `uv run pytest tests/unit/agents/ -q` (without `--ignore`) fails with 1 collection error. The tests were written for the future non-stub implementation (Finding 1 scope), but they should either be gated behind a skip marker or removed until the activity is implemented.

---

## New issues found during review

### Issue A (medium): test_activities.py is broken against current code
As described above, `test_activities.py` imports `compute_pod_name` which does not exist in `temporal_activities.py`, and calls `run_sandbox_step` with a `spawner` keyword that the current signature does not accept. Running `uv run pytest tests/unit/agents/ -q` without ignoring this file produces a collection error. This is not a regression from the fixes -- it appears to be forward-looking test code for the sandbox implementation (Finding 1) -- but it means the full test suite does not pass cleanly.

### Issue B (low): Pydantic deprecation warnings in workflow tests
The Temporal test suite produces 30+ warnings about deprecated Pydantic v1 methods (`dict`, `parse_obj`). These come from `temporalio`'s converter layer and are not actionable in this codebase, but the message suggests using `temporalio.contrib.pydantic.pydantic_data_converter` for cleaner Pydantic v2 integration.

---

## Outstanding findings (not addressed -- noted only)

- **Finding 1:** Sandbox activity remains a stub. `run_sandbox_step` returns hard-coded `{"status": "completed"}`. No spawn, no HTTP call, no retry classification.
- **Finding 4:** API/entrypoint contract is still partial. No `WORKFLOW_ENGINE` switching, no SSE events endpoint, no workflow-name resolution.

---

## Summary

All three addressed findings (2, 3, 5) are **fully resolved**:

| Finding | Status | Evidence |
|---------|--------|----------|
| 5 (lint) | PASS | `ruff check` returns 0 errors |
| 2 (auth) | PASS | `build_temporal_router` accepts and applies `auth_dependency`; test proves 401 on all 4 endpoints |
| 3 (conditions/parallel) | PASS | Uses `conditions.py` evaluator, fails closed on invalid conditions, parallel_group implemented; all 3 aspects have passing tests |
| Old code deletion | PASS | All 16 files deleted, 10 kept files present, no dangling imports (361 tests pass) |

One new issue surfaced: `test_activities.py` is broken because it references symbols that don't exist yet (`compute_pod_name`, `spawner` parameter). This does not block the addressed fixes but should be resolved before the next review round.
