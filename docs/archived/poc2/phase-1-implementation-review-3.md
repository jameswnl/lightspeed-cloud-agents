# Review: PoC2 Phase 1 fix verification

Scope reviewed: commit `97f7c4ba` after the initial implementation review in `phase-1-implementation-review-1.md`.

## Findings

### 1. High: the real Temporal app still does not enforce auth or support workflow-name resolution
`src/agents/workflow/temporal_api.py` added optional `auth_dependency` and `definition_store` parameters, but `src/agents/workflow/temporal_entrypoint.py` still builds the router with only `build_temporal_router(placeholder_client)`. In the actual app, that means:

- `/v1/workflows/*` is still exposed without the required stack auth dependency
- `POST /v1/workflows/run` with `workflow_name` still cannot work because no `DefinitionStore` is injected, so the handler falls into its `"workflow_name requires a definition store"` 400 path

This matters because the previous review finding was about the deployed trust boundary and public API contract, not just helper signatures. Right now the helper is fixed, but the running app still behaves like the original broken version.

Recommended fix: wire `get_auth_dependency()` and a real `DefinitionStore` through `build_temporal_app()`, then add entrypoint-level tests that prove unauthenticated requests are rejected and `workflow_name` runs resolve successfully.

### 2. High: the activity still drops prior-step state, so the Phase 1 sandbox context contract is not actually implemented
`src/agents/workflow/temporal_activities.py` now calls `build_sandbox_context()`, but it does so with `workflow_steps={}` and ignores the accumulated step results already passed in under `input["context"]`. As a result, the runtime path can never populate the context sections that Phase 1 explicitly requires:

- `previousAttempts`
- `approvedOption`
- `executionResult`

The standalone helper tests in `tests/unit/agents/workflow/temporal/test_context.py` pass, but the actual activity path still cannot send those fields to the sandbox request body.

Recommended fix: reconstruct `StepResult` objects from `input["context"]` before calling `build_sandbox_context()`, or build the sandbox context before dispatching the activity. Add activity-level tests that assert the posted `/v1/agent/run` payload includes approved-option and execution-result context when prior steps exist.

### 3. Medium: prompt interpolation and initial workflow input are still not wired into execution
The prior review also called out that the Temporal workflow did not use the existing interpolation and input-prompt machinery. That remains true in `src/agents/workflow/temporal_workflow.py`:

- nothing calls `interpolation.py`
- `WorkflowInput.input_prompt` is never used after entering `run()`
- the activity request still sends `step.get("prompt", "")` directly

This means workflows that rely on `{{ steps.X.output.Y }}` prompt templates or on the initial user prompt will not execute according to the approved design, even though the tests all pass.

Recommended fix: interpolate step prompts before dispatch, thread `input_prompt` into the first-step context or prompt construction per the phase design, and add regression tests that prove a step can consume prior-step output in its prompt.

### 4. Medium: sandbox readiness failures are still mishandled
`run_sandbox_step()` awaits `spawner.wait_ready(endpoint)` but ignores its boolean return value. If readiness times out and returns `False`, the code still proceeds to POST to the sandbox endpoint anyway instead of failing immediately as an infrastructure error.

That weakens the failure contract because a readiness timeout becomes an indirect downstream HTTP failure instead of a clear "sandbox never became ready" retry condition.

Recommended fix: check the `wait_ready()` result explicitly and raise an infrastructure error before attempting the HTTP call when readiness fails.

## Perspective Check
- Functionality: remaining gaps. The fix commit improves the Temporal scaffolding, but the real app wiring and the runtime context/prompt contracts are still incomplete.
- Quality: mixed. Cleanup was substantial and the claimed test/lint commands pass, but the passing tests do not yet prove the actual entrypoint/runtime seam behavior.
- Security: remaining gap. The live Temporal entrypoint still does not attach the required auth dependency.

## Verification
- Inspected git context with:
  - `git log --oneline --decorate 0bbc6c79cc82371ca8b7d8a05a7cfe4300e421cc..HEAD`
  - `git diff --name-only 0bbc6c79cc82371ca8b7d8a05a7cfe4300e421cc..HEAD`
  - `git show --stat --summary --decorate 97f7c4ba25540e87937e40eb4187b26959541ae9`
- Read the updated implementation and matching tests in:
  - `src/agents/workflow/temporal_activities.py`
  - `src/agents/workflow/temporal_api.py`
  - `src/agents/workflow/temporal_entrypoint.py`
  - `src/agents/workflow/temporal_workflow.py`
  - `src/agents/workflow/temporal_context.py`
  - `src/agents/workflow/definition.py`
  - `tests/unit/agents/workflow/temporal/*`
- Searched for dangling imports of deleted workflow modules in `src/` and `tests/` and found none.
- Ran:
  - `uv run pytest tests/unit/agents/workflow/temporal -q` -> `54 passed`
  - `uv run pytest tests/unit/agents/ -q` -> `368 passed`
  - `uv run ruff check src/agents/workflow/temporal_*.py tests/unit/agents/workflow/temporal` -> passed

## Summary
This commit meaningfully improves the branch: the cleanup landed, the Temporal-focused tests now pass, and several helper-level fixes are real. But the key runtime seam is still not review-clean. The deployed Temporal app remains effectively unauthenticated, workflow-name execution is not actually wired, and the activity path still discards the prior-step context that Phase 1 depends on.
