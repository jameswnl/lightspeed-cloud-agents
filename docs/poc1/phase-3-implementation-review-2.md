# Review: Phase 3 Implementation (Updated)

## Findings

### 1. Major: approval rejection semantics still do not match the approved design

The new commit correctly implements lazy approval-timeout enforcement and adds the missing persistence of final completed state. Those were real improvements.

But the implementation still handles explicit rejection (`approved=False`) differently from the approved design:

- the design says a rejected approval step should mark the workflow as **failed** and stop
- the implementation marks the approval step as **completed** with `{"approved": false}`, then continues the executor from the next step
- any later step guarded by `steps.approval.approved == true` is skipped
- the workflow can therefore finish as **completed**

#### Why this matters

This is not a cosmetic difference. It changes the observable contract:

- user-facing workflow status
- automation behavior that keys off `completed` vs `failed`
- audit semantics for rejected workflows

A rejected destructive workflow should not look like a successful completed workflow unless that is an intentional product decision.

#### Recommendation

If the approved design is still the intended contract, then `resume(..., approved=False)` should:

1. mark the approval step as failed
2. set workflow status to failed
3. stop immediately

Right now the implementation still diverges from the plan on that point.

## What Improved

This commit did close several important gaps from the first Phase 3 implementation review:

- approval timeout is now enforced lazily in `get_state()` and `resume()`
- final completed workflow state is persisted
- workflow API auth is now better covered by tests
- workflow entrypoint tests were added

Those are meaningful fixes and improve confidence in the workflow engine.

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow -q
```

Result:

- **61 passed**

## Summary

The Phase 3 implementation is closer to matching the approved design now, but I still would not mark it fully approved yet because the explicit rejection path still has the wrong workflow outcome semantics.

If rejection-to-failed behavior gets aligned with the design and covered by tests, I would expect this review to be ready for final approval.
