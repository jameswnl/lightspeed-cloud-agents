# Review: Phase 4a Implementation (Commit `fc73399a`)

## Findings

### 1. Major: the workflow API authentication gap is still open

This commit adds useful hardening work:

- NetworkPolicy manifests
- ServiceAccount + RBAC manifests
- a validation guard on `max_retries`

But it does **not** change the workflow API authentication model in code.

The workflow runner API still behaves as before:

- `POST /v1/workflows/run` — open
- `GET /v1/workflows/{id}` — open
- `GET /v1/workflows` — open
- `POST /v1/workflows/{id}/approve` — token-gated only

#### Why this matters

The original Phase 4a implementation review remains valid:

- the phase goal is “full API authentication (all endpoints)”
- the current implementation still only fully covers the generic agent runtime
- this commit improves network containment, but does not complete the workflow control-plane auth story

That means the core Phase 4a auth milestone is still unfinished in code.

#### Recommendation

The next implementation slice should add shared auth middleware (or equivalent) to the workflow runner endpoints so the workflow API matches the hardened Phase 4a contract.

## What Improved

This commit is still good progress:

- Kubernetes-side containment is stronger
- per-agent service accounts and workflow-runner job permissions are now explicitly modeled
- `setup.sh` now applies RBAC and NetworkPolicy before deploying agents
- `max_retries` now has a lower-bound validation guard

Those are valuable Phase 4a hardening pieces even though they do not close the remaining auth gap.

## Perspective Check

- Functionality: improved manifest/setup correctness and workflow config validation
- Quality: hardening work is being layered in cleanly; targeted tests still pass
- Security: improved network and RBAC posture, but workflow API endpoint auth is still incomplete

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/runtime/test_auth.py -q
```

Result:

- **16 passed**

## Summary

This is a worthwhile Phase 4a hardening commit, but it does not resolve the main remaining implementation concern:

- full workflow API authentication is still not implemented

So Phase 4a implementation is still **not** at `LGTM` yet.
