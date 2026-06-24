# Phase 5: pydantic-graph Fit Assessment

## Summary

pydantic-graph's `GraphBuilder` API was evaluated as an alternative workflow executor alongside the existing `WorkflowExecutor`. The assessment found that pydantic-graph is a **partial fit** — strong for graph topology and parallel execution, but fundamentally mismatched for durable pause/resume and state persistence.

## Fit Matrix

| Feature | Rating | Notes |
|---------|--------|-------|
| Linear step execution | Natural fit | Steps → nodes → edges works cleanly |
| Conditional branching | Works with adaptation | Decision nodes possible but conditions live in step functions, not graph topology |
| Approval pause/resume | Impedance mismatch | GraphRun is an async generator — cannot be serialized and resumed after process restart |
| Retry with escalation | Works with adaptation | Retry loop lives inside step function, not in graph semantics |
| Parallel (Fork/Join) | Natural fit | pydantic-graph's Fork/Join is type-safe with proper fan-out/fan-in — superior to hand-rolled asyncio.gather |
| Ephemeral spawn | Works with adaptation | Spawn/destroy lifecycle lives in step function, transparent to graph |
| Durable persistence | Impedance mismatch | GraphBuilder has no built-in state persistence. Cannot survive pod restarts. Fundamental gap. |
| Event emission | Works with adaptation | Events emitted from step functions, not from graph topology |
| Advisory mode | Natural fit | Step function checks deps.advisory — clean injection via DepsT |
| Mermaid visualization | Natural fit | Graph topology renders to Mermaid automatically |

## Key Findings

### What works well

1. **Graph topology construction** — `GraphBuilder.step()` + `add_edge()` maps cleanly from our `WorkflowStepSpec` list. Dynamic construction at load time works.
2. **Type-safe state** — `StateT` (GraphWorkflowState) and `DepsT` (GraphWorkflowDeps) provide typed access to workflow state and dependencies inside step functions.
3. **Step function factories** — `make_agent_step_fn(spec)` and `make_approval_step_fn(spec)` create closures that capture the step spec and work naturally with `StepContext`.

### What doesn't fit

1. **Durable persistence** — The existing `WorkflowExecutor` persists `WorkflowState` to PostgreSQL and can resume after pod restarts. `GraphExecutor` holds the `GraphRun` async generator in memory. This is a fundamental architectural mismatch, not a gap we can close.
2. **Approval pause/resume** — The `GraphExecutor.resume()` method works by replaying remaining steps sequentially (not via graph continuation). This is a workaround, not a graph-native solution.
3. **Graph structure validation** — pydantic-graph validates graph structure (edges from start, edges to end) which fights dynamic construction where some steps may be conditional.

### API stability

pydantic-graph v1.105.0 (shipped with pydantic-ai). The `BaseNode`/`Graph` API is deprecated. `GraphBuilder` is the replacement but is relatively new. API churn risk is moderate.

## Recommendation

**Keep `WorkflowExecutor` as the production executor.** It supports durable persistence, approval pause/resume across restarts, and has been battle-tested through Phases 3-4.

**Use pydantic-graph selectively for:**
- Visualization — `Graph.mermaid_code()` is free and useful
- Future parallel execution — Fork/Join is genuinely superior
- Agent-internal graphs — individual agents (not workflows) could use pydantic-graph for multi-step reasoning within a single agent run

**Do not use pydantic-graph for:**
- Workflow orchestration with durable state
- Long-running workflows that must survive restarts
- Workflows with human-in-the-loop approval gates

## Architecture Decision

The `GraphExecutor` remains available behind `WORKFLOW_EXECUTOR=graph` for exploration and visualization. The `WorkflowExecutorProtocol` ensures both executors share the same interface. Production deployments use the default `WorkflowExecutor`.
