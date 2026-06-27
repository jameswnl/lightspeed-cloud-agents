# Phase 5: pydantic-graph Exploration + Ephemeral Step Execution

## Context

Phases 1-4c built a cloud agents framework with a hand-rolled sequential workflow executor. Phase 5 explores whether **pydantic-graph** is a natural fit for workflow execution, and makes **ephemeral pod execution the default** for workflow steps.

**Key finding from research:** pydantic-graph's `BaseNode`/`Graph` API is deprecated. The `GraphBuilder` API is the replacement. However, GraphBuilder is designed for static DAGs, while our workflows are YAML-driven, interruptible (approval pause/resume), and dynamically constructed. This means pydantic-graph is **not a drop-in replacement** — we build it as an alternative executor alongside the current one, compare, and document the fit.

**Approach:** Build a `GraphExecutor` in parallel with the existing `WorkflowExecutor`. Both implement the same Protocol. A comparison test suite proves behavioral equivalence or documents divergence. The fit assessment determines the production path.

**Known impedance mismatches (from evaluator review):**
- **Persistence:** pydantic-graph GraphBuilder has no built-in state persistence. Our existing executor supports durable persistence (PostgreSQL) across pod restarts. GraphExecutor's in-memory async generator cannot survive restarts. This is a fundamental gap.
- **API stability:** pydantic-graph v2.0.0 is very recent. Pin the version explicitly in pyproject.toml.

**Process:** TDD, every task gets an independent opus evaluator.

### Implementation outcome

Phase 5 was completed as a **linear-graph-only exploration** with **partial SpawnConfig support**. The following items from the original plan were **not implemented** and are deferred:

- **Task 2 (partial):** Decision nodes for conditional routing — conditions handled inside step functions instead
- **Task 5:** Fork/Join parallel execution — not built, remains theoretical fit
- **Task 7 (partial):** SpawnConfig enforcement in PodmanSpawner, wait_ready health_path/timeout, agent-level envelope precedence
- **Task 9 (partial):** Full parametrized comparison suite — GraphExecutor tests mirror WorkflowExecutor scenarios but not via shared parametrized fixture

See `phase-5-pydantic-graph-assessment.md` for the fit matrix and recommendation.

---

## Task 1: Executor Protocol + Graph State Models + Dependency Setup

**Why first:** Defines the shared contract both executors must satisfy, sets up pydantic-graph as an explicit dependency, and creates the bridge types.

**Design:**
- Add `pydantic-graph` as explicit dependency in `pyproject.toml` (pin version)
- `WorkflowExecutorProtocol` — formal Protocol with `run()`, `resume()`, `get_state()`, `list_workflows()` (runtime methods only, not construction)
- Refactor existing `WorkflowExecutor` to explicitly satisfy the Protocol
- `GraphWorkflowState` (dataclass) — pydantic-graph `StateT` wrapping our `WorkflowState`
- `GraphWorkflowDeps` (dataclass) — pydantic-graph `DepsT` with registry, spawner, advisory, persistence, etc.

**Files:**
- Modify: `pyproject.toml` — add `pydantic-graph` dependency with version pin
- Create: `src/agents/workflow/executor_protocol.py`
- Modify: `src/agents/workflow/executor.py` — verify satisfies Protocol
- Create: `src/agents/workflow/graph_state.py`
- Create: `tests/unit/agents/workflow/test_graph_state.py`

---

## Task 2: Dynamic Graph Builder — WorkflowDefinition → pydantic-graph Graph

**Why:** The core translation layer. Dynamically constructs a `GraphBuilder` graph from a YAML-driven `WorkflowDefinition`.

**Design:**
- `build_workflow_graph(definition)` — iterates `WorkflowStepSpec` list, creates Step nodes, Decision nodes (for conditions), Fork/Join (for parallel groups)
- Each agent step becomes a `GraphBuilder.step()` with a factory-generated async function
- Each approval step becomes a step that returns an `ApprovalNeeded` sentinel
- Conditions become `Decision` nodes routing to step or skip
- Parallel groups become `Fork` → N steps → `Join`

**Files:**
- Create: `src/agents/workflow/graph_builder_factory.py`
- Create: `tests/unit/agents/workflow/test_graph_builder_factory.py`

**Tests:** Build graphs from 1-step, multi-step, conditional, parallel, and approval definitions. Verify node count, edge structure, Mermaid rendering.

---

## Task 3: Agent + Approval Step Functions

**Why:** The step functions that run inside pydantic-graph nodes — agent dispatch with spawn/retry, and approval with auto-approve/pause.

**Design:**
- `make_agent_step_fn(step_spec)` — factory returning `async def(ctx: StepContext) -> StepResult`. Handles interpolation, spawn (ephemeral by default), retry with `RetryContext`, advisory mode.
- `make_approval_step_fn(step_spec)` — factory returning approval step. Checks advisory skip, auto-approve policy, returns `ApprovalNeeded` sentinel for manual approval.

**Files:**
- Create: `src/agents/workflow/graph_steps.py`
- Create: `tests/unit/agents/workflow/test_graph_steps.py`

**Tests:** Mock RemoteAgentClient and spawner. Test agent step success/failure/retry/escalation. Test approval step auto-approve and pause trigger.

---

## Task 4: GraphExecutor — Main Executor Class

**Why:** The alternative executor that uses pydantic-graph to run workflows, same interface as `WorkflowExecutor`.

**Design:**
- `GraphExecutor` — constructor takes same args as `WorkflowExecutor`, builds graph via `build_workflow_graph()`
- `run()` — creates `GraphWorkflowState`, runs graph via `Graph.iter()`, handles `ApprovalNeeded` by pausing
- `resume()` — restores paused state, continues graph execution
- Approval pause: hold `GraphRun` async generator in memory. **GraphExecutor is scoped to same-process exploratory execution only** — it cannot survive pod restarts or process recycling. This is an intentional limitation for the fit exploration, not a production gap to close later.
- `WorkflowExecutor` remains the production executor with durable persistence across restarts.

**Files:**
- Create: `src/agents/workflow/graph_executor.py`
- Create: `tests/unit/agents/workflow/test_graph_executor.py`

**Tests:** Mirror all `test_executor.py` scenarios: single step, two-step, failure, conditions, approval pause/resume, rejection, retry, escalation, spawner, advisory mode. Durability tests (persist + restart + resume) are **excluded** — GraphExecutor is not expected to pass them.

---

## Task 5: Parallel Execution via Fork/Join

**Why:** pydantic-graph's Fork/Join is the strongest fit argument — proper fan-out/fan-in with typed state merging.

**Design:**
- Extend `graph_builder_factory.py`: consecutive steps with same `parallel_group` → `Fork` node → N parallel step nodes → `Join` node
- Join uses a reducer that merges `StepResult` dicts back into `WorkflowState.steps`
- Fail-fast: if any parallel step fails, cancel others
- **Reduced subset:** Phase 5 evaluates Fork/Join with fail-fast only. The full Phase 4c parallel contract (fail-fast vs continue strategy, same-agent warnings, retry independence within groups) is the target for the comparison suite but not all invariants may map cleanly to pydantic-graph's Fork/Join. Divergences are documented in the fit assessment, not forced into the graph model.

**Files:**
- Modify: `src/agents/workflow/graph_builder_factory.py`
- Create: `tests/unit/agents/workflow/test_graph_parallel.py`

**Tests:** Two parallel steps succeed, one fails (workflow fails), parallel followed by sequential, parallel with conditions.

---

## Task 6: Ephemeral-by-Default Spawn Semantics

**Why:** KubeKlaw L5 — each step should run in its own isolated sandbox pod by default. Applies to BOTH executors.

**Design:**
- Change `WorkflowStepSpec.spawn` default from `"pre-deployed"` to `"ephemeral"`
- Rename `"on-demand"` to `"ephemeral"` for clarity; keep `"on-demand"` as alias
- Both executors respect the new default
- **Fail closed:** When `spawn == "ephemeral"` and no spawner is configured, raise a validation error at workflow load time. The workflow author must explicitly opt into `spawn: pre-deployed` if no spawner is available. No silent fallback — silent fallback would undermine the isolation guarantee.

**Files:**
- Modify: `src/agents/workflow/definition.py` — change default, add alias
- Modify: `src/agents/workflow/executor.py` — update spawn check to `in ("ephemeral", "on-demand")`
- Modify: `src/agents/workflow/graph_steps.py` — same logic in graph steps
- Update: existing tests that rely on `spawn == "pre-deployed"` default or `"on-demand"` literal
- Create: `tests/unit/agents/workflow/test_ephemeral_spawn.py`

---

## Task 7: Spawner Enhancements for Ephemeral Lifecycle

**Why:** Ephemeral-by-default needs per-step resource configuration.

**Design:**
- `SpawnConfig` model: `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit`, `timeout_seconds`, `health_path`
- **Validation bounds** on `SpawnConfig` to prevent resource abuse (e.g., max 4 CPU, max 4Gi memory)
- **Precedence rule:** `AgentDefinition.spec.resources` sets the maximum allowed envelope. `SpawnConfig` may request a narrower per-step override within that envelope. Any request exceeding the agent-level limits is rejected at validation time. This is one rule, not a merge.
- Add optional `spawn_config` to `WorkflowStepSpec`
- Both spawners accept `SpawnConfig` in `_do_spawn`
- Container cleanup robustness: metric for cleanup failures (`ls_spawn_cleanup_errors_total`), log warning on failed destroy
- Prometheus metrics: `ls_spawn_duration_seconds`, `ls_step_teardown_duration_seconds`

**Files:**
- Modify: `src/agents/spawner/base.py` — add `SpawnConfig`
- Modify: `src/agents/spawner/kubernetes_spawner.py` — accept per-step resources
- Modify: `src/agents/spawner/podman_spawner.py` — accept per-step resources
- Modify: `src/agents/workflow/definition.py` — add `spawn_config` field
- Create: `tests/unit/agents/spawner/test_ephemeral_lifecycle.py`

---

## Task 8: Entrypoint Integration + Executor Selection

**Why:** Wire GraphExecutor as a selectable alternative.

**Design:**
- `WORKFLOW_EXECUTOR=default|graph` env var selects executor
- Both implement `WorkflowExecutorProtocol`
- API layer works unchanged

**Files:**
- Modify: `src/agents/workflow/entrypoint.py` — add executor selection
- Create: `tests/unit/agents/workflow/test_executor_selection.py`

---

## Task 9: Comparison Test Suite

**Why:** The definitive evaluation artifact — proves behavioral equivalence or documents divergence.

**Design:**
- Parametrized pytest fixture: `@pytest.fixture(params=["default", "graph"])`
- Same scenarios against both executors
- Asserts matching `state.status`, `state.steps`, outputs

**Files:**
- Create: `tests/unit/agents/workflow/test_executor_comparison.py`

**Observable contract:** Only user-authored workflow steps (from the YAML) are externally visible. Internal graph nodes (Decision, Fork, Join) do NOT emit public `WorkflowEvent`s. For parallel groups, compare ordering constraints (all steps in group started before any completes) rather than a strict total order.

**Scenarios:**
- Linear workflow, conditions (true/false), approval (pause/resume/reject/timeout)
- Retry (success/exhaustion), parallel group, ephemeral spawn, advisory mode
- Concurrent workflow runs, event emission (ordering constraints, not strict order)
- **Behavioral equivalence:** matching `state.status`, `state.steps` keys/statuses, outputs
- **Durability parity (separate):** persistence round-trip (save → restart → resume). GraphExecutor is expected to fail this — document as known limitation in assessment.

---

## Task 10: Fit Assessment Document

**Why:** Document exploration findings for the team.

**Files:**
- Create: `docs/design/cloud-agents/phase-5-pydantic-graph-assessment.md`

**Content:**
- Fit matrix: each feature rated as "natural fit" / "works with adaptation" / "impedance mismatch"
- Must evaluate: conditions, approval pause/resume, retry/escalation, parallel (Fork/Join), ephemeral spawn, **durable persistence across restarts**, event emission, advisory mode
- Mermaid diagram from `Graph.render()`
- Recommendation for production path
- Version stability assessment (pydantic-graph v2 is very recent)
- Migration path if adopted

---

## Task Dependencies

```
Task 1 (Protocol + State) → Task 2 (Graph Builder) → Task 3 (Step Functions) → Task 4 (GraphExecutor) → Task 5 (Parallel)
                                                                                       │
Task 6 (Ephemeral) → Task 7 (Spawner) ────────────────────────────────────────────────┘
                                                                                       │
                                                                                       v
                                                                                 Task 8 (Entrypoint)
                                                                                       │
                                                                                       v
                                                                                 Task 9 (Comparison)
                                                                                       │
                                                                                       v
                                                                                 Task 10 (Assessment)
```

- Tasks 1→2→3→4 are the core graph executor chain. Task 5 (parallel) extends Task 4.
- Tasks 6→7 (ephemeral) can run in parallel with Tasks 2-4.
- Task 8 merges both tracks.

---

## Verification

```bash
uv run pytest tests/unit/agents/ -q                                    # platform tests
uv run pytest examples/tests/ -q                                       # example tests
uv run pytest tests/unit/agents/workflow/test_executor_comparison.py -v # comparison
```
