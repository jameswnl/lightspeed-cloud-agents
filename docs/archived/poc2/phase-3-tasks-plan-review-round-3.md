# Review: `phase-3-tasks.md`

## Findings

### 1. Major (Quality): T0 still contradicts the env-var source-of-truth contract

The contract section now clearly says only `LIGHTSPEED_PROVIDER` and `LIGHTSPEED_MODEL` come from `ProviderConfig`, while the other five sandbox vars come from deployment config on the workflow-runner and are propagated from `os.environ`. But T0 step 1 still says to "set all 7 env vars from ProviderConfig," which directly conflicts with the contract text above it.

This is the kind of second-order contradiction that causes implementation drift: one engineer will extend `ProviderConfig`, another will read from deployment env, and both can claim they followed the doc.

Recommended fix:
- update T0 step 1 to match the contract exactly
- say that `LIGHTSPEED_PROVIDER` and `LIGHTSPEED_MODEL` come from `ProviderConfig`
- say that `LIGHTSPEED_MODEL_PROVIDER`, `LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, `LIGHTSPEED_PROVIDER_REGION`, and `LIGHTSPEED_PROVIDER_API_VERSION` come from workflow-runner deployment env and are forwarded to sandbox pods
- keep the credential wording aligned with the companion credential contract (`credentials_secret` resolves to provider-specific env vars via `SecretKeyRef` or Podman host env propagation)

## Perspective Check

- Functionality: no new major issues found
- Quality: remaining inconsistency in the env-var source of truth inside the doc itself
- Security: no new major issues found beyond the explicitly deferred single-team boundary

## Open Questions / Assumptions

- I assumed the contract section is the intended final source of truth and T0 step 1 is the stale line.
- I assumed `ProviderConfig` is intentionally unchanged in this phase.

## Summary

The plan is very close. The remaining issue is small but real: T0 still contradicts the clarified env-var contract, so the doc is not yet fully self-consistent.
