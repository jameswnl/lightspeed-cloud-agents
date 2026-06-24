# Review: phase-5-tasks.md

## Findings

### 1. Blocker: GraphExecutor cannot satisfy the existing durable pause/resume contract
Task 4 says `GraphExecutor` will expose the same runtime interface as `WorkflowExecutor`, but its approval pause/resume mechanism depends on holding a `GraphRun` async generator in memory. That is not compatible with the current workflow persistence model, which persists `WorkflowState` and resumes later by `workflow_id`. After a process restart or pod replacement, there is no persisted graph cursor or continuation to restore.

This matters because the plan still presents `GraphExecutor` as a peer executor behind the same protocol and selector path. That is only true for same-process execution. It is not true for workflows that must survive restarts, which Phase 4b made part of the platform contract.

Recommended fix: explicitly scope `GraphExecutor` to same-process exploratory execution, or add a concrete persisted continuation/cursor design. Also split “behavioral equivalence” from “durability parity” in Task 9.

### 2. Major: Ephemeral-by-default is undermined by silent fallback to pre-deployed
Task 6 changes the default spawn mode to `ephemeral`, but it also says that when no spawner is configured the step should fall back to `pre-deployed` with only a warning.

This matters because it weakens the trust boundary the phase is trying to establish. A workflow author can believe a step is isolated in its own sandbox while it is actually running in a shared long-lived runtime.

Recommended fix: fail closed. If the default is `ephemeral`, missing spawner support should be a validation/runtime error unless the workflow explicitly opts into `spawn: pre-deployed`.

### 3. Major: spawn_config creates a second source of truth for runtime limits
Phase 4 established that spawned runtimes inherit resource limits from the `AgentDefinition`. Task 7 adds a per-step `SpawnConfig` with CPU, memory, timeout, and health settings, but it does not define precedence or merge rules relative to the existing agent-level contract.

This matters because implementation and tests will drift unless the precedence model is explicit. One path may treat `spawn_config` as an override, another as a cap, and another may continue using only the agent-level settings.

Recommended fix: define one clear rule. The cleanest version is that `AgentDefinition` sets the maximum allowed envelope, and `spawn_config` may request a narrower per-step override inside that envelope. Any request outside the envelope should be rejected.

### 4. Major: The comparison suite asks for event-order equivalence without defining the observable graph contract
Task 9 requires both executors to match on outputs and event emission order, but the graph design introduces internal runtime nodes like decisions, forks, and joins. The plan never says whether those are externally visible, how skipped conditions map to events, or what “same order” means once parallel branches exist.

This matters because equivalence is not meaningful unless the external contract is normalized. Otherwise the comparison suite will either fail on internal implementation differences or pass without testing the right thing.

Recommended fix: define that only user-authored workflow steps are externally observable. Internal graph nodes should not emit public workflow events. For parallel groups, compare ordering constraints rather than a single strict total order.

### 5. Medium: Phase 5 parallel semantics drift from the existing Phase 4c contract
Phase 4c already defined parallel-group semantics in more detail: barrier behavior, same-agent warnings, retry independence, and `fail-fast` versus `continue`. Task 5 narrows this to a `Fork`/`Join` design with fail-fast cancellation, but it does not say whether that is an intentional reduction for the exploration or the new canonical behavior.

This matters because both executors need to target the same workflow language if the comparison is going to be credible.

Recommended fix: either pull the Phase 4c parallel invariants directly into this plan, or explicitly say Phase 5 is evaluating a reduced subset of the workflow language and list the unsupported or changed semantics.

## Perspective Check
- Functionality: blocker remains around durable pause/resume and restart survival for `GraphExecutor`; observable equivalence is also under-specified.
- Quality: source-of-truth and semantic-alignment issues remain around `spawn_config` and parallel execution.
- Security: the silent fallback from `ephemeral` to `pre-deployed` weakens the intended isolation boundary.

## Open Questions / Assumptions
- Is `GraphExecutor` intended only for exploratory evaluation, or for normal runtime selection?
- Is restart durability out of scope for the graph path, or required for parity?
- Should `spawn_config` be allowed to widen an agent's resource profile, or only narrow it?
- Is Phase 5 evaluating the full workflow language or only a subset needed for fit assessment?

## Summary
The overall direction is sound: evaluating pydantic-graph alongside the current executor is safer than a rewrite. The main issue is that the plan currently reads closer to drop-in parity than the design supports. Tightening the persistence contract, the spawn trust boundary, the config precedence, and the comparison semantics would make the evaluation much more credible.
