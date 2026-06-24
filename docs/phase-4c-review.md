# Review: `phase-4c-tasks.md`

## Findings

### 1. Major: the “cherry-pickable” claim conflicts with the stated file overlap and task ordering

**Primary perspective:** quality

The doc says all 11 items are cherry-pickable, but later it explicitly calls out heavy overlap in a few core files, especially:

- `src/agents/workflow/executor.py`
- `src/agents/workflow/definition.py`
- `src/agents/runtime/generic_runner.py`
- `src/agents/runtime/server.py`

In practice, several tasks are only cherry-pickable at the roadmap level, not at the implementation level:

- Task 6 (SSE events) and Task 10 (parallel execution) both deeply reshape `executor.py`
- Task 7 (notifications), Task 8 (escalation packaging), and Task 3 (advisory mode) all want to hook workflow state transitions in the same executor
- Task 5 (MCP tools) and Task 9 (permission scoping) both touch runtime/agent construction

#### Why this matters

This makes the plan look more modular than it really is and can hide merge-order and sequencing pressure.

#### Recommendation

Reframe “cherry-pickable” as:

- product-level optionality, not implementation independence
- identify task clusters that should be implemented together
- call out which tasks are truly standalone vs executor-centric

### 2. Major: Advisory mode is too weakly enforced for the guarantee implied by “read-only mode”

**Primary perspective:** functionality / security

The design says advisory mode appends prompt text like “Do NOT execute remediation,” skips approval steps, and marks outputs as advisory. It also explicitly notes this relies on prompt engineering and does not filter tools.

#### Why this matters

If a workflow step still has write-capable tools, “advisory mode” is not actually read-only. It is only a best-effort instruction, not an enforced mode.

That creates a contract mismatch between the feature name and the actual guarantee.

#### Recommendation

Either:

- rename it to something like “advisory prompting mode,” or
- make Phase 4c advisory mode require actual tool filtering for write/destructive capabilities

### 3. Major: MCP tool support introduces a new secret-handling and config-trust surface that the task plan understates

**Primary perspective:** security

Task 5 adds `mcp_servers` in YAML with optional `headers`. The note says headers in YAML should really use K8s secrets, but the task breakdown does not include a concrete secret-reference or safe injection mechanism.

#### Why this matters

Without a concrete secret model, users are likely to:

- put bearer tokens directly in YAML
- treat mounted config as a safe secret channel
- widen the config-trust surface without guardrails

That is especially risky because MCP endpoints are external control surfaces, not just local helpers.

#### Recommendation

The Phase 4c task should either:

- include a proper secret-reference mechanism now, or
- explicitly restrict `headers` to dev/test and defer production MCP auth until a safe secret model exists

### 4. Major: Parallel step execution needs stronger constraints than “no cross-references within a group”

**Primary perspective:** functionality / quality

The design says same-`parallel_group` steps can run concurrently, with validation preventing cross-references within a group and banning approval steps in groups.

That still leaves several ambiguous cases:

- two steps mutating the same external system without referencing each other
- two steps depending on shared global state or tool side effects
- retries and escalation behavior when one branch fails mid-group
- how `continue` strategy interacts with later steps that consume partial group outputs

#### Why this matters

This is the first time the workflow executor stops being purely sequential. The correctness model needs to be stronger than “no obvious reference cycle.”

#### Recommendation

Add explicit invariants for parallel groups:

- parallel steps should be side-effect-safe or read-only unless explicitly documented otherwise
- later steps may only run after the whole group reaches a terminal group state
- define exactly how group outputs are represented when some steps fail and others succeed

### 5. Medium: AI-generated workflow design is underspecified for draft storage and approval lifecycle

**Primary perspective:** functionality / quality

Task 11 says the designer agent generates valid `WorkflowDefinition` YAML and a rationale, and a human reviews before execution. But it does not yet say:

- where the generated workflow is stored before approval
- whether it enters version control, runtime state, or temporary memory
- how approval differs from immediate execution
- whether generated workflows must pass the same validation and persistence rules as handwritten ones before review

#### Why this matters

This makes the “human reviews before execution” step thinner than it needs to be. Review and storage semantics matter for generated infrastructure logic.

#### Recommendation

Clarify a minimum lifecycle:

1. generate workflow draft
2. validate structurally
3. persist draft in a reviewable location
4. human approves or rejects
5. only then allow execution or deployment

## Perspective Check

- Functionality: remaining gaps around advisory-mode guarantees, parallel semantics, and generated-workflow lifecycle
- Quality: remaining gaps around “cherry-pickable” realism and shared-file/task coupling
- Security: remaining gaps around MCP secret handling and the implied guarantees of read-only/advisory mode

## Open Questions / Assumptions

1. Is “cherry-pickable” meant as product prioritization only, not implementation independence?
2. Should advisory mode actually remove or filter write-capable tools?
3. Are MCP auth headers intended for production in this phase, or just for dev/test experimentation?
4. Is AI-generated workflow output meant to become durable config before approval, or just an ephemeral proposal?

## Summary

The Phase 4c task plan is strong overall and much more concrete than a typical “advanced features” backlog. The main issues are not with ambition, but with guarantees: a few features are named or scoped more strongly than their enforcement model currently supports.

If I were tightening this next, I’d focus first on:

1. making advisory mode either truly read-only or clearly weaker in name/scope
2. strengthening the semantics of parallel execution
3. defining a safe secret model for MCP integration
