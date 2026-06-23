# Review: Phase 3 Implementation

## Findings

### 1. Major: approval timeout behavior from the design is not implemented in the executor or API

The approved Phase 3 design says approval timeouts are enforced lazily on read:

- `GET /v1/workflows/{id}`
- `POST /v1/workflows/{id}/approve`
- `resume()`

should all check whether the current approval step has exceeded `timeout_seconds` and fail the workflow with `"approval timed out"`.

The implementation does not do that.

#### Why this matters

Right now:

- `WorkflowExecutor.get_state()` just returns the stored state
- `WorkflowExecutor.resume()` resumes immediately if the workflow is paused
- `api.py` simply delegates to those methods

So an approval step can remain paused forever and still be approved long after its timeout should have expired.

That is a semantic mismatch with the approved design and a real control-flow bug, not a missing polish item.

#### Recommendation

Implement a shared timeout-enforcement check and call it from:

- `get_state()`
- `resume()`
- the approve API path before processing approval

### 2. Major: rejected approvals do not fail the workflow as the design says

The approved Phase 3 design says:

- if the human rejects (`approved: false`), the workflow marks the approval step as **failed** and stops

But the implementation currently marks the approval step as **completed** with `{"approved": false}`, then allows the next step’s condition to skip execution and the workflow ends as **completed**.

#### Why this matters

That changes workflow semantics in a meaningful way:

- design: rejection is a terminal failure
- implementation: rejection is a successful workflow completion with a skipped downstream step

That affects:

- user-facing workflow state
- observability and alerting
- any future automation that treats `completed` and `failed` differently

#### Recommendation

If the approved design is still the intended contract, `resume(..., approved=False)` should set the workflow to failed and stop immediately instead of continuing through the step list.

### 3. Major: the workflow API trust boundary from the design is only partially implemented

The approved design explicitly documents:

- approve endpoint authenticated
- run/state/list endpoints unauthenticated in Phase 3 dev/test

The implementation only enforces auth on approval, which matches the design, but it does **not** encode any other trust-boundary guardrail in code or deploy-time checks.

#### Why this matters

This means the effective runtime behavior is:

- any caller that can reach the pod can start workflows
- any caller can read workflow state
- any caller can list active workflows

That may be acceptable for dev/test, but because the implementation contains no visible guardrail besides the approval token, it is easy for this to be reused outside that context while preserving the same open control plane.

This is more of a security design-to-implementation gap than a code bug, but it is still worth calling out in the review.

#### Recommendation

At minimum, make the dev/test-only nature of the open workflow API explicit in deployment assets and startup logging, or add an opt-in guard so the open mode is a deliberate choice rather than an ambient default.

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow -q
```

Result:

- **54 passed**

The test suite is internally clean, but it also reflects the current implementation semantics — including the approval-rejection behavior that differs from the design.

## Summary

The Phase 3 implementation is coherent and well-covered at the unit level, but I do not think it fully matches the approved design yet.

The biggest remaining issues are semantic:

1. approval timeouts are designed but not implemented
2. approval rejection semantics differ from the design
3. the workflow API trust boundary is only lightly enforced in practice

So this is solid implementation progress, but I would not treat the Phase 3 code as fully aligned with the approved plan yet.
