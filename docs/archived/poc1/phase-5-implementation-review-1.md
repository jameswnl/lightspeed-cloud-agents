# Review: Phase 5 implementation

## Findings

### 1. Blocker: The graph path never implements the Fork/Join or decision topology the phase claims to evaluate
The Phase 5 plan and assessment present pydantic-graph's graph topology, especially Fork/Join, as the core thing being explored. But `build_workflow_graph()` still builds a simple linear chain from `start -> step1 -> step2 -> ... -> end`, and it ignores `parallel_group` entirely. Conditions are also not modeled as graph structure; they are still handled inside the step function body.

This matters because the implementation does not actually exercise the strongest claimed fit area. The assessment currently says parallel Fork/Join is a "natural fit", but the committed code never builds or runs a Fork/Join graph, so that conclusion is not supported by this phase's implementation.

Recommended fix: either implement real decision/Fork/Join topology plus the promised comparison tests, or narrow the assessment and phase-complete claim to say only linear graph execution was explored.

### 2. Major: `WORKFLOW_EXECUTOR=graph` drops workflow event streaming semantics
The API layer is wired to pass an `event_callback` into the selected executor for SSE streaming, and the default `WorkflowExecutor` emits `WorkflowEvent`s throughout execution. `GraphExecutor` stores the callback in deps, but neither `GraphExecutor` nor `graph_steps.py` ever emit any events.

This matters because the seam between the workflow API and executor selection is broken: selecting the graph executor means `/v1/workflows/run/stream` no longer provides step progress events. The whole workflow test suite still passes because there is no test covering graph-mode streaming or event emission.

Recommended fix: either implement the same externally observable event contract in the graph path or explicitly disable graph mode for streaming endpoints until parity exists.

### 3. Major: Ephemeral-by-default still silently degrades to shared execution when no spawner is configured
The approved Phase 5 plan was updated to fail closed: `spawn: ephemeral` should error if no spawner is available. The implementation did not follow that contract. `WorkflowStepSpec.spawn` now defaults to `"ephemeral"`, but both executors still fall back to `client_factory(...)` when no spawner is configured.

This matters because it weakens the intended isolation boundary. A workflow author can believe a step is running in its own ephemeral sandbox while it is actually being dispatched to a pre-deployed shared endpoint.

Recommended fix: validate at workflow load time or raise at execution time whenever an `ephemeral` step is present without a configured spawner.

### 4. Major: `SpawnConfig` is dead code, so Task 7's per-step lifecycle controls are not actually implemented
`SpawnConfig` was added in `src/agents/spawner/base.py`, but it is not threaded into `WorkflowStepSpec`, the spawner interface, or any spawner implementation. There is no `spawn_config` field in the workflow definition model, and `AgentSpawner.spawn()` still only accepts `(agent_name, image, env)`.

This matters because the phase claims per-step resource configuration and precedence rules, but the runtime cannot receive or enforce any of them. This is exactly the kind of "field/model exists but is never used" gap that can make a phase look more complete than it is.

Recommended fix: either finish the end-to-end wiring for `spawn_config` and its validation/enforcement path, or remove/defer the claim from the phase completion narrative.

### 5. Medium: The committed assets and test surface do not match the promised Phase 5 deliverables
The phase plan called for explicit `pydantic-graph` dependency pinning plus new tests/files for builder behavior, graph steps, executor selection, comparison coverage, and parallel behavior. The current range does not add a `pydantic-graph` entry to `pyproject.toml`, and the promised test files for builder/steps/selection/comparison/parallel are absent.

This matters because the code is being marked complete without the verification and dependency hardening the plan said were part of the phase. The passing workflow test suite is real, but it does not prove the missing Phase 5 contracts above.

Recommended fix: either add the missing dependency/test assets, or update the phase scope and completion claim to reflect what was actually implemented.

## Perspective Check
- Functionality: significant gaps remain in graph topology evaluation, event streaming parity, and ephemeral execution semantics.
- Quality: the test suite passes, but it does not cover several Phase 5 contracts; some committed models/assets are not wired into runtime behavior.
- Security: the silent fallback from `ephemeral` to shared execution remains a trust-boundary regression.

## Verification
- Reviewed full phase range: `e2f64191045b803c582b89e0a55872e83304f086..HEAD`
- Commands run:
  - `git log --oneline --decorate e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff --name-only e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff --stat e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `uv run pytest tests/unit/agents/workflow/test_graph_state.py tests/unit/agents/workflow/test_graph_executor.py -q` -> `12 passed`
  - `uv run pytest tests/unit/agents/workflow/test_executor.py -q` -> `18 passed`
  - `uv run pytest tests/unit/agents/workflow -q` -> `171 passed`

## Summary
The phase adds a real exploratory `GraphExecutor`, but the implementation does not yet support several of the behaviors the Phase 5 plan and assessment say were evaluated or completed. The biggest gaps are missing graph topology support (especially Fork/Join), missing event-stream parity, and the still-open silent fallback from `ephemeral` to shared execution. This is not ready for `LGTM` yet.
