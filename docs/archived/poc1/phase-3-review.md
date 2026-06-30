# Review: `phase-3-workflow-design.md`

## Findings

### 1. Blocker: approval timeouts are not actually implementable with the pause-and-return execution model as written

The design says a `human-approval` step pauses the workflow, persists state, and returns until `resume()` is called later. It also says that if the timeout expires, the approval step is marked failed with error `"approval timed out"`.

Those two statements do not fit together yet.

#### Why this matters

Once the executor:

- hits an approval step
- persists state
- returns control to the caller

there is no active execution loop left running that can notice wall-clock timeout expiry on its own.

That means the design is missing one of the following:

- a background sweeper/reaper that marks timed-out approvals as failed
- timeout checks embedded in `get_state()` / `list()` / `resume()`
- a pydantic-graph node/persistence model that natively wakes/re-evaluates timeouts

Without one of those, `"approval timed out"` is an intended behavior with no mechanism behind it.

#### Recommendation

Add an explicit timeout-enforcement mechanism to the design. Right now this is a blocker because the approval semantics are incomplete.

### 2. Major: the persistence story is underspecified and does not obviously fit the executor architecture being proposed

The design repeatedly references `pydantic-graph FileStatePersistence`, but the executor itself is described as a hand-rolled sequential loop over YAML steps, not a pydantic-graph node graph.

#### Why this matters

There are two different architectural models mixed together:

- **graph-native persistence** via `pydantic-graph`
- **custom sequential executor** with its own `WorkflowState`

Those models can both work, but they imply different implementation strategies, especially for:

- checkpointing
- pause/resume
- timeout enforcement
- restart semantics

Right now the document gets the advantages of graph persistence without committing to the graph execution model that would make those advantages real.

#### Recommendation

Choose one explicitly:

- **Use pydantic-graph as the execution substrate**
- or **define a custom persistence interface for the sequential executor**

Until that is clarified, the persistence section is more aspirational than executable.

### 3. Major: the workflow runner deployment contract is still ambiguous

The design says the workflow runner is itself “an agent pod running on `agent-runtime:latest`,” but the deployment section then says:

- use the same image
- mount `workflow.yaml`
- set `WORKFLOW_MODE=true`
- **or** use a dedicated workflow entrypoint

That is still two different runtime contracts.

#### Why this matters

For implementation and operations, this is a real ambiguity:

- does the existing generic entrypoint branch on `WORKFLOW_MODE=true`?
- or is there a separate workflow-specific entrypoint?
- if it is the same image, what exactly does startup look for first: `agent.yaml` or `workflow.yaml`?

Until that is settled, Task 9 (“Workflow runner entrypoint”) is not well-bounded.

#### Recommendation

Pick one concrete runtime contract:

- **Same image, dedicated workflow entrypoint**
- **Same entrypoint, mode flag**

but not both in the same plan.

### 4. Major: cross-step prompt interpolation creates a prompt-injection/data-poisoning surface that the security section understates

The security section says:

- prompt templates interpolate values as strings via regex
- “no code injection possible”

That is true in a narrow code-execution sense, but it misses the more relevant risk: **one step’s output becomes instructions-like text in a later step’s prompt**.

#### Why this matters

If an upstream agent output contains adversarial or malformed text, it can shape the next agent step’s prompt in unintended ways.

This is not arbitrary Python execution, but it is still a real control-plane risk in a workflow system where:

- steps call LLM-backed agents
- later prompts are built from earlier outputs
- outputs may include free-form natural language summaries

The design currently treats interpolation as structurally safe when it is only syntactically safe.

#### Recommendation

Update the security model to acknowledge prompt-injection risk explicitly, and consider one of:

- only interpolating allowlisted structured fields
- separating “data fields” from “narrative summary fields”
- rendering interpolated values inside clearly delimited data blocks

### 5. Medium: the template and condition grammars are still narrower than the workflow examples imply

The design examples use workflow outputs like:

- `steps.recommend.output.actions_taken`
- `steps.diagnose.output.issues_found`

The interpolation function only supports one output key:

```python
{{ steps.X.output.Y }}
```

and the condition parser similarly supports a shallow `output.<key>` form.

That is workable for many cases, but the prose says nested values are handled explicitly, which is stronger than what the shown implementation actually supports.

#### Why this matters

This is not a blocker, but it will show up quickly when workflow authors try to do more complex things with:

- nested objects
- arrays of objects
- richer outputs from future custom agents

#### Recommendation

Either:

- narrow the documented promise to match the one-level grammar, or
- expand the grammar/design to support deeper paths explicitly

## Summary

The Phase 3 direction is strong: a declarative workflow executor is the right next step after generic single-agent runtime support.

The biggest remaining weaknesses are not in the overall vision, but in the control-plane semantics:

1. approval timeout enforcement
2. persistence model choice
3. workflow runner startup contract
4. prompt-injection handling across step outputs

If those are tightened, the plan will be much closer to implementation-ready.
