# Review: Phase 5 implementation follow-up

## Findings

### 1. Blocker: The graph topology claim is still not implemented
`build_workflow_graph()` still constructs only a linear chain from `start -> step1 -> step2 -> ... -> end`. It does not build Decision nodes for conditional routing and it still ignores `parallel_group`, so there is no actual Fork/Join topology in the committed Phase 5 implementation.

This still matters because the phase assessment and completion narrative are about evaluating pydantic-graph as a fit for workflow topology. The assessment was improved to acknowledge that only linear topology was implemented, which fixes the documentation mismatch, but the implementation still does not deliver the topology exploration that was originally scoped as the core technical experiment.

Recommended fix: either implement real Decision / Fork / Join graph construction and matching tests, or explicitly close Phase 5 as a linear-graph-only exploration and defer true topology evaluation to a later phase/task.

### 2. Major: `SpawnConfig` is still only partially enforced end-to-end
The new commit wires `spawn_config` further through the runtime and into the spawner interface, which is a real improvement. But the implementation is still incomplete relative to the Phase 5 contract:

- `PodmanSpawner` accepts `config` but does not use it for CPU/memory limits
- `AgentSpawner.wait_ready()` still hardcodes `/healthz` and `60s`, so `health_path` and `timeout_seconds` are not enforced
- the documented precedence rule against agent-level resource envelopes is still not enforced anywhere in runtime code

This matters because the phase still claims per-step resource, timeout, and health controls with a clear precedence rule, but only part of that contract is implemented.

Recommended fix: either finish the Podman/health/timeout/envelope enforcement path, or narrow the phase text to the subset that is actually implemented today.

## Perspective Check
- Functionality: the main remaining gap is the still-missing graph topology exploration beyond a linear chain.
- Quality: `SpawnConfig` wiring improved, but the runtime still does not fully implement the documented per-step lifecycle contract.
- Security: no new major security concerns were found in this follow-up pass; the earlier fail-closed `ephemeral` behavior remains intact.

## Verification
- Re-reviewed full phase range: `e2f64191045b803c582b89e0a55872e83304f086..HEAD`
- Compared this pass against the remaining findings from `phase-5-implementation-review-2.md`
- Commands run:
  - `git log --oneline --decorate e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff --name-only e2f64191045b803c582b89e0a55872e83304f086..HEAD`
  - `git diff 5a48d05606c06db795838dc6c0c2343051bb7656..HEAD -- src/agents/workflow/graph_steps.py src/agents/spawner/base.py src/agents/spawner/kubernetes_spawner.py src/agents/spawner/podman_spawner.py src/agents/workflow/graph_executor.py docs/design/cloud-agents/phase-5-pydantic-graph-assessment.md tests/unit/agents/spawner/test_base.py tests/unit/agents/workflow/test_graph_executor.py`
  - `uv run pytest tests/unit/agents/workflow/test_graph_executor.py tests/unit/agents/spawner/test_base.py tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_entrypoint.py -q` -> `28 passed`
  - `uv run pytest tests/unit/agents/workflow tests/unit/agents/spawner/test_base.py -q` -> `178 passed`

## Summary
This follow-up commit resolved part of the remaining review-2 concerns: the assessment now accurately scopes the topology work, step-level graph events were added, and `spawn_config` is threaded farther into the runtime. The phase is still not ready for `LGTM`, though, because the actual topology exploration remains linear-only and the documented `SpawnConfig` contract is still only partially implemented.
