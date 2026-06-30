# Review: `phase-3-workflow-design.md` (Updated)

## Findings

### 1. Major: the workflow runner deployment contract is still ambiguous

The updated plan materially improves timeout and persistence semantics, but the workflow runner startup contract is still split between two possibilities:

- same image with `WORKFLOW_MODE=true`
- or a dedicated workflow entrypoint that loads `workflow.yaml` instead of `agent.yaml`

#### Why this matters

This is still a real implementation ambiguity:

- does the current generic entrypoint branch on `WORKFLOW_MODE=true`?
- or is there a workflow-specific entrypoint?
- what does the container look for first at startup: `agent.yaml` or `workflow.yaml`?

This affects:

- Task 9 scope
- deployment docs
- health/startup behavior
- how reuse of `agent-runtime:latest` is actually achieved

#### Recommendation

Pick one concrete runtime contract and state it as the Phase 3 design decision:

- **same image, dedicated workflow entrypoint**
- or **same entrypoint, mode flag**

Right now the plan is still carrying both.

### 2. Major: prompt interpolation is still treated as structurally safe, but the design still understates prompt-injection risk

The updated design improves control-plane semantics, but the security section still frames prompt interpolation mainly as:

- regex substitution
- no code injection

That misses the more relevant workflow-specific risk: upstream agent output becomes downstream prompt content.

#### Why this matters

Even if interpolation is not arbitrary code execution, it is still a control surface:

- step 1 can emit free-form narrative text
- step 2 embeds that text into a new agent prompt
- a malformed or adversarial output can shape the next step’s behavior

This matters especially in a system designed to chain LLM-backed agent steps.

#### Recommendation

The plan should explicitly acknowledge prompt-injection/data-poisoning risk across steps and define at least one mitigation strategy, for example:

- only interpolate allowlisted structured fields
- distinguish narrative fields from machine-usable fields
- wrap interpolated values in clear data boundaries

### 3. Medium: the deployment and task sections still do not clearly connect to the security model

The design now has stronger execution semantics, but the workflow API introduces another privileged control surface:

- start workflow
- inspect workflow state
- approve workflow execution

The security section adds bearer-token auth for approvals, which is good, but there is still no explicit statement about whether:

- `/v1/workflows/run`
- `/v1/workflows/{id}`
- `/v1/workflows`

are also authenticated in Phase 3.

#### Why this matters

If only the approval endpoint is protected, the remaining workflow control plane may still allow:

- unauthorized workflow submission
- workflow state disclosure
- enumeration of active workflows

That is not necessarily wrong for dev/test, but the trust boundary should be explicit.

#### Recommendation

Clarify whether Phase 3 secures:

- only approval
- or the full workflow API surface

If it is intentionally minimal, say so clearly in the trust model.

## What Improved

The updated plan is significantly better than the first version:

- approval timeout enforcement is now actually implementable
- persistence is now aligned with a custom sequential executor instead of vaguely borrowing pydantic-graph semantics
- the earlier blocker around “paused but no timeout mechanism” is resolved

Those changes remove the biggest control-plane ambiguity from the original draft.

## Summary

The Phase 3 plan is now much closer to implementation-ready.

I no longer see the earlier approval-timeout or persistence-model problems as blocking issues. The main remaining issues are narrower:

1. workflow runner startup contract
2. explicit treatment of prompt-injection across workflow steps
3. clearer trust boundary for the full workflow API surface

If those are tightened, I would expect the design to be in strong shape.
