# Review: Phase 5 implementation follow-up

## Findings

### 1. Blocker: The graph path still does not implement the Fork/Join or decision topology the phase claims to evaluate
The follow-up commit fixed several seam issues, but `build_workflow_graph()` is still a simple linear chain from `start -> step1 -> step2 -> ... -> end`. It does not build decision nodes for conditions, and it still ignores `parallel_group` entirely.

This matters because the central Phase 5 claim is that pydantic-graph was explored as a fit for workflow topology, with Fork/Join described as the strongest fit argument. The committed implementation still does not exercise that topology, so the assessment cannot support a conclusion about graph-native parallel execution.

Recommended fix: either implement real decision/Fork/Join graph construction plus the matching tests, or narrow the phase completion and assessment text to say the exploration only covered linear graph execution.

### 2. Major: Graph-mode event streaming is still missing step-level parity
`GraphExecutor` now emits workflow-level events (`workflow.started`, `workflow.paused`, `workflow.completed` / `workflow.failed`), which fixes part of the prior seam break. But it still does not emit step-level events like `step.started`, `step.completed`, `step.failed`, or `step.skipped`, and there is no graph-mode API/streaming test that proves equivalent externally observable behavior.

This matters because selecting `WORKFLOW_EXECUTOR=graph` still changes the SSE event contract that callers see. The default executor emits per-step progress; the graph path does not.

Recommended fix: emit the same step-level `WorkflowEvent` contract from the graph path and add a graph-mode streaming/API test that proves the parity.

### 3. Major: `SpawnConfig` is only partially wired and still not enforced by the spawner interface
The follow-up commit improved this by adding `spawn_config` to `WorkflowStepSpec`, but the runtime still does not pass it into `AgentSpawner.spawn()` or `_do_spawn()`, and there is no enforcement path in the spawners for the promised per-step resource/timeout/health configuration.

This matters because the phase still claims per-step lifecycle/resource controls and precedence rules, but the runtime cannot actually apply them to spawned workloads. The model exists, but the behavior is not implemented end-to-end.

Recommended fix: extend the spawner interface to accept `spawn_config`, thread it through both executors, and enforce the documented precedence rule in the actual spawner implementations.

## Perspective Check
- Functionality: remaining gaps are concentrated in graph topology support and graph-mode event behavior.
- Quality: several prior issues were fixed, but the runtime/test surface still does not prove the promised Fork/Join and per-step spawn-config contracts.
- Security: the earlier silent fallback from `ephemeral` to shared execution appears fixed; no new major security issues were found in this follow-up pass.

## Verification
- Re-reviewed full phase range: `e2f64191045b803c582b89e0a55872e83304f086..HEAD`
- Compared follow-up changes against previous review findings
- Commands run:
  - `git log --oneline --decorate e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff --name-only e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff d608eab5dff8b7444859130c423f4c7a4a9b7db0..HEAD -- pyproject.toml src/agents/workflow/executor.py src/agents/workflow/graph_executor.py src/agents/workflow/definition.py src/agents/spawner/base.py tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_graph_executor.py`
  - `uv run pytest tests/unit/agents/workflow/test_graph_executor.py tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_entrypoint.py -q` -> `21 passed`
  - `uv run pytest tests/unit/agents/workflow -q` -> `171 passed`

## Summary
The follow-up commit fixed real issues from the previous review: `ephemeral` now fails closed when no spawner is configured, and the dependency/test surface is better than before. But the phase is still not ready for `LGTM` because the core graph-topology claim remains unimplemented, graph-mode streaming still lacks step-level parity, and `spawn_config` is not enforced end-to-end.
