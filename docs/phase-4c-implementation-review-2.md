# Review: Phase 4c Implementation (Commit `6c5e8ffe`)

## Findings

### 1. Major: the branch still has a failing interpolation regression, so the implementation is not in a clean state

The new commit focuses on advisory mode, but the targeted Phase 4c interpolation suite is currently failing.

Specifically:

- `interpolate()` still raises on a missing nested intermediate key
- the existing test `test_nested_missing_intermediate_returns_null` still expects `<data>null</data>`

That means the branch currently has an unresolved mismatch between implementation behavior and test expectations.

#### Why this matters

This is a **functionality** and **quality** issue:

- a Phase 4c implementation review cannot move toward approval while the targeted suite is red
- it is not yet clear whether the intended contract is “fail fast on invalid nested path” or “coerce missing nested path to null”
- until that decision is made and code/tests are aligned, the interpolation feature is unstable

#### Recommendation

Decide the intended Phase 4c contract for missing nested interpolation paths, then make code and tests agree:

- if the contract is fail-fast, update the test
- if the contract is null-coercion, restore the catch-and-coerce logic in `interpolate()`

## Perspective Check

- Functionality: advisory-mode work may be progressing, but interpolation behavior remains unresolved
- Quality: targeted test suite is failing, so the Phase 4c branch is not review-clean
- Security: no new security regression was identified in this slice; the immediate blocker is correctness/test health

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow/test_interpolation.py -q
```

Result:

- **1 failed, 24 passed**

The failing test was:

- `TestNestedInterpolation.test_nested_missing_intermediate_returns_null`

## Summary

A new Phase 4c implementation commit landed, but I cannot advance toward LGTM because the targeted interpolation suite is currently failing.

The watcher should continue, but the next step needs to resolve the interpolation contract mismatch before broader review can continue confidently.
