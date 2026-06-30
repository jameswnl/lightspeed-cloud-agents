# Review: PoC2 Phase 1 implementation batch

Scope reviewed: commit `0bbc6c79` plus the current working-tree follow-up changes in the Temporal Phase 1 files.

## Findings

### 1. High: the sandbox activity is still a stub, so the Phase 1 runtime path is not actually implemented
`src/agents/workflow/temporal_activities.py` returns a hard-coded `{"status": "completed"}` payload and never performs any of the behavior that Phase 1 is supposed to deliver: no spawn, no readiness wait, no HTTP call to `/v1/agent/run`, no 502-vs-application-failure split, no context building, no label-based cleanup, and no pre-deployed path. As written, the branch cannot exercise the advertised FastAPI -> Temporal -> sandbox pod -> LLM -> structured result flow on either Kind or Podman.

This matters because most of the phase contract lives at that seam. The current tests pass because they only prove that the stub returns a canned success shape, not that the workflow can actually talk to a sandbox runtime safely or correctly.

Recommended fix: implement the real activity contract from Tasks 8-10, then add contract tests for success, HTTP 502 retry, HTTP 200 application failure, cancellation cleanup, and pre-deployed execution.

### 2. High: the new `/v1/workflows/*` Temporal endpoints are unauthenticated
`src/agents/workflow/temporal_api.py` exposes start, approve, query, and cancel routes without `get_auth_dependency()` or any equivalent auth hook. The Phase 1 plan explicitly requires stack auth on all endpoints, but the current router would allow any caller that can reach the service to start workflows, send approval signals, inspect workflow state, and cancel runs.

This is the main new trust boundary in the phase, so leaving it open is a real security regression even if the feature is otherwise incomplete.

Recommended fix: add the same auth dependency pattern used by the rest of the stack to every Temporal route and add API tests that verify unauthenticated callers are rejected.

### 3. High: `AgentWorkflow` does not implement several of the approved execution semantics and silently weakens condition handling
`src/agents/workflow/temporal_workflow.py` still executes the workflow as a simple sequential loop. It does not implement `parallel_group`, does not use the existing `interpolation.py`, does not use `auto_approve.py`, and now uses a custom `_evaluate_condition()` helper instead of the existing safe evaluator in `conditions.py`. That helper only understands a narrow `steps.X.output.Y == value` shape and returns `True` for anything it cannot parse.

That combination creates real behavior drift from the reviewed Phase 1 design. In particular, unsupported conditions can now fail open and run guarded steps instead of stopping them, and workflows that rely on existing condition syntax or parallel groups will not behave like the design and tests claim.

Recommended fix: wire the workflow back to the existing condition/interpolation helpers, implement parallel-group execution before claiming Task 7 done, and fail closed on invalid condition syntax.

### 4. Medium: the API/entrypoint contract is still partial, so Temporal mode is not actually interchangeable with legacy mode yet
The reviewed batch adds `temporal_api.py`, `temporal_worker.py`, and `temporal_entrypoint.py`, but it does not complete the Task 12-14 surface:

- no `WORKFLOW_ENGINE=temporal|legacy` integration in `src/agents/workflow/entrypoint.py`
- no `/v1/workflows/{id}/events` SSE endpoint
- `POST /v1/workflows/run` only accepts an inline definition and separate provider fields instead of supporting workflow-name resolution
- `src/agents/workflow/definition.py` adds step-level fields but still omits the top-level `provider` and `skills` additions called for in the Phase 1 task list

This means the branch does not yet deliver the "same v1 API surface regardless of engine" contract the plan commits to.

Recommended fix: finish the entrypoint/router definition wiring, then add tests that exercise both legacy and Temporal engine selection against the same public v1 routes.

### 5. Medium: the current verification is too shallow, and the new Temporal files are not lint-clean
The focused Temporal unit suite passes, but it mostly validates simplified or stubbed behavior. It does not prove the runtime contracts that matter most for this phase: auth, real sandbox dispatch, retry classification, cleanup, workflow-name resolution, engine switching, or end-to-end event streaming. On top of that, a targeted `ruff check` already fails on the new Temporal files/tests for banned `unittest` usage in tests, unused imports, and import-order issues.

So the batch is not yet at a "tests prove the feature" quality bar even before broader integration testing starts.

Recommended fix: make the new Temporal files lint-clean, replace the stub-centric tests with contract tests at the workflow/API seam, and defer LGTM until at least the focused Temporal suite proves the real runtime path.

## Perspective Check
- Functionality: major gaps remain. The core sandbox execution path, parallel execution support, and parts of the promised API surface are not implemented yet.
- Quality: major gaps remain. The current tests mostly validate a simplified happy path, and targeted linting already fails on the new Temporal files.
- Security: major gap found. The new Temporal workflow control endpoints are currently unauthenticated.

## Verification
- Read the reviewed Phase 1 task file and compared the code against the approved Phase 1 contracts.
- Inspected branch/worktree scope with:
  - `git status --short`
  - `git log -6 --stat --decorate --oneline`
  - `git diff --name-only`
- Ran:
  - `uv run pytest tests/unit/agents/workflow/temporal -q` -> `44 passed`
  - `uv run ruff check src/agents/workflow/temporal_activities.py src/agents/workflow/temporal_api.py src/agents/workflow/temporal_context.py src/agents/workflow/temporal_entrypoint.py src/agents/workflow/temporal_models.py src/agents/workflow/temporal_worker.py src/agents/workflow/temporal_workflow.py tests/unit/agents/workflow/temporal` -> failed with 26 issues

## Summary
This is a useful start on the Temporal scaffolding, but it is not review-ready as a completed Phase 1 implementation batch. The biggest blockers are that the sandbox activity is still stubbed, the new API surface is unauthenticated, and the workflow semantics still fall short of the reviewed design.
