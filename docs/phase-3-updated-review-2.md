# Review: `phase-3-workflow-design.md` (Updated Again)

## Findings

### 1. Major: the full workflow API trust boundary is still not explicit enough

The updated plan now clearly chooses a deployment contract and explicitly acknowledges prompt-injection risk in cross-step interpolation. Those were good fixes.

The main remaining issue is that the security section only specifies authentication for the **approval endpoint** via `WORKFLOW_APPROVAL_TOKEN`. It still does not explicitly state whether:

- `POST /v1/workflows/run`
- `GET /v1/workflows/{workflow_id}`
- `GET /v1/workflows`

are also authenticated in Phase 3 or intentionally left open.

#### Why this matters

Those endpoints are part of the workflow control plane:

- `run` can start new workflows
- `get_state` can reveal all step outputs
- `list` can expose active workflow inventory

If only approvals are authenticated, then the trust model is still incomplete:

- unauthorized workflow submission may be possible
- sensitive workflow state may be readable
- active workflow enumeration may be exposed

This might be acceptable for a dev/test phase, but it needs to be stated explicitly.

#### Recommendation

Clarify one of these:

- **Option A:** the entire workflow API requires the same bearer token
- **Option B:** only approval is authenticated in Phase 3, and run/state/list are trusted-internal-only

Either is defensible for a phased design, but the choice should be explicit.

## What Improved

Compared with the prior draft, the plan is substantially stronger:

- approval timeout enforcement is now implementable
- persistence is now aligned with a custom sequential executor
- the workflow runner startup contract is now clear: same image, dedicated workflow entrypoint
- prompt-injection risk is now explicitly acknowledged and partially mitigated with data delimiters and field restrictions
- the grammar scope is now honest about one-level output paths only

Those changes remove most of the earlier ambiguity.

## Summary

The Phase 3 plan is close to review-ready.

I no longer see the earlier timeout, persistence, startup-contract, or grammar-scope issues as blockers.

The one remaining concern is the **scope of authentication for the workflow API surface**. Once that trust boundary is made explicit, I would expect the design to be in strong shape.
