# Review: Phase 4c Implementation (Commit `cc821bc8`)

## Findings

### 1. Major: nested interpolation silently returns `<data>null</data>` on invalid paths instead of failing fast as the plan specifies

The Phase 4c task plan explicitly said nested interpolation should raise `ValueError` on invalid paths, consistent with existing behavior.

The implementation adds `resolve_path()` that correctly raises `ValueError` for:

- missing keys
- out-of-range indices
- type mismatches

But `interpolate()` catches that `ValueError` and converts it to `None`, which then renders as `<data>null</data>`.

#### Why this matters

This is a **functionality** and **quality** mismatch with the approved plan:

- broken workflow templates no longer fail fast
- invalid references degrade silently into `"null"`
- debugging template mistakes becomes harder
- later workflow steps may proceed with semantically broken prompts

The new tests also lock in this behavior, which means the code and tests now agree with each other while diverging from the design.

#### Recommendation

If the plan is still the source of truth, `interpolate()` should propagate `ValueError` from `resolve_path()` rather than swallowing it.

### 2. Medium: `<data>...</data>` wrapping is weaker for plain strings than for structured values

The interpolation layer wraps:

- dicts/lists using `json.dumps(...)`
- booleans using JSON `true` / `false`

but plain strings are inserted directly:

```python
return f"<data>{value}</data>"
```

#### Why this matters

This is mainly a **security** concern:

- a string value containing `</data>` or similar delimiter-shaped content can break the intended prompt-boundary convention
- the design’s prompt-injection mitigation relies on those delimiters being trustworthy separators

This is not arbitrary code execution, but it does weaken the prompt-structure safety story for free-form string outputs.

#### Recommendation

Consider serializing string values in a safer way, for example:

- JSON-encoding them as string payloads inside the data block
- or escaping delimiter-like content before insertion

## Perspective Check

- Functionality: nested interpolation feature is implemented, but invalid-path behavior currently diverges from the task plan
- Quality: tests are strong for the shipped behavior, but that behavior appears to contradict the documented contract
- Security: prompt-boundary handling for plain strings is weaker than for structured values

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow/test_interpolation.py -q
```

Result:

- **25 passed**

## Summary

This is a good first Phase 4c implementation slice, but I would not mark it `LGTM` yet because the nested interpolation error behavior appears to have drifted from the approved task plan, and the plain-string prompt-wrapping story still looks weaker than intended.
