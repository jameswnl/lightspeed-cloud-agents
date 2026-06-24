# Review: `phase-4c-tasks.md`

## Findings

### 1. Major: the “all 11 items are cherry-pickable” framing is misleading given the shared implementation hot spots

**Primary perspective:** quality

The doc says all 11 items are cherry-pickable, but it also explicitly identifies a few files that multiple tasks will modify, especially:

- `src/agents/workflow/executor.py`
- `src/agents/workflow/definition.py`
- `src/agents/runtime/generic_runner.py`
- `src/agents/runtime/server.py`

That means these items are only cherry-pickable at the roadmap/prioritization level, not at the implementation level. In practice:

- Task 6 (SSE), Task 7 (notifiers), Task 8 (escalation packaging), and Task 10 (parallel execution) all want to alter workflow state transitions in `executor.py`
- Task 5 (MCP tools) and Task 9 (permission scoping) both alter runtime/tool loading semantics
- Task 3 (advisory mode) also touches execution semantics in the same core loop

#### Why it matters

This can create hidden sequencing and merge complexity while making the plan look more modular than it really is.

#### Recommendation

Reword “cherry-pickable” to mean product optionality, not engineering independence, and group the work into implementation clusters:

- executor/state-transition cluster
- runtime/tool-loading cluster
- observability cluster

### 2. Major: advisory mode still over-promises compared to the enforcement model described

**Primary perspective:** functionality / security

The plan calls Task 3 “Advisory/Read-Only Mode,” but the enforcement model is:

- append advisory text to prompts
- skip approval steps
- annotate outputs with `advisory: true`

The doc also explicitly admits it relies on prompt engineering and that production should filter tools to read-only tools.

#### Why it matters

That means “read-only” is not actually guaranteed in this phase. If write-capable tools are still present, this is advisory behavior, not enforced read-only behavior.

#### Recommendation

Either:

- rename the feature to something like `advisory mode`, or
- make tool filtering part of the Phase 4c task, not just a future production note

Right now the name implies a stronger guarantee than the implementation model supports.

### 3. Major: MCP support introduces a secret/config trust problem that is still under-scoped

**Primary perspective:** security

Task 5 adds MCP server support in `agent.yaml`, including optional `headers`. The doc does note that headers in YAML should use K8s secrets, but the task itself does not include:

- a secret reference model
- a mount/injection strategy
- validation rules separating dev/test and production usage

#### Why it matters

Without that, the path of least resistance becomes:

- putting bearer tokens directly in YAML
- treating runtime config as a secret transport
- expanding the sensitive config surface without clear guardrails

That is especially risky because MCP endpoints are external control surfaces, not just local helpers.

#### Recommendation

Either:

- add a secret reference mechanism to the task now, or
- explicitly limit header-based MCP auth to dev/test and defer production-safe secret handling

### 4. Major: parallel step execution needs stronger correctness rules than “no cross-references within a group”

**Primary perspective:** functionality / quality

The proposed validation says:

- same `parallel_group` steps run concurrently
- no cross-references within a group
- no approval steps in a group

That still leaves several hard cases unspecified:

- two parallel steps mutating the same external system
- two steps depending on shared global/tool side effects
- mixed success/failure handling under `parallel_fail_strategy`
- when later steps may read outputs from a partially failed group

#### Why it matters

This is the first real transition from sequential workflow semantics to concurrent execution. The plan needs stronger invariants than “no obvious reference cycle.”

#### Recommendation

Add explicit rules such as:

- parallel groups should be side-effect-safe unless explicitly allowed
- downstream steps only run after the full group reaches a terminal group state
- define the group result model when one branch fails and another succeeds

### 5. Medium: AI-generated workflow design still lacks a concrete draft lifecycle

**Primary perspective:** functionality / quality

Task 11 says the designer agent generates valid workflow YAML and a rationale, with human review before execution. But it still doesn’t fully specify:

- where generated drafts are stored
- whether they become repo files, runtime state, or transient artifacts
- whether approval means “approve execution once” or “approve as a reusable workflow definition”
- whether generated workflows must pass the same validation gates as handwritten ones before review

#### Why it matters

Without a concrete draft lifecycle, “human reviews before execution” is underspecified and could drift into ad hoc behavior.

#### Recommendation

Define a minimum lifecycle:

1. generate workflow draft
2. validate schema + allowed features
3. persist draft in a reviewable location
4. human approves or rejects
5. only then allow execution or promotion into reusable config

## Perspective Check

- Functionality: remaining gaps in advisory-mode guarantees, parallel semantics, and generated-workflow lifecycle
- Quality: remaining gaps in the “cherry-pickable” framing and shared-file coupling across tasks
- Security: remaining gap around MCP header/secret handling and the strength of the “read-only” claim in advisory mode

## Open Questions / Assumptions

1. Is “cherry-pickable” intended as product-priority flexibility only, not implementation independence?
2. Should advisory mode actually filter tools, or is it intentionally just prompt-level behavior in 4c?
3. Are MCP headers expected to be usable in production in this phase, or only in dev/test?
4. Is a designer-generated workflow an ephemeral execution proposal or a new durable workflow definition?

## Summary

On a clean pass, `phase-4c-tasks.md` is a strong and concrete plan overall. The main issues are not with ambition, but with guarantees and engineering realism.

The two biggest areas I’d tighten first are:

1. make advisory mode’s guarantee honest and enforceable
2. strengthen the execution model for parallel groups

After that, I’d clarify the MCP secret model and the lifecycle for generated workflow drafts.
