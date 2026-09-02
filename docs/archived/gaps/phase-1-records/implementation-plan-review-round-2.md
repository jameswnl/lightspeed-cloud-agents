# Review: Phase 1 implementation plan

## Findings

### 1. Major (Quality, Security): the `Sandbox-side work` section is now stale relative to the authenticated callback contract
The main `T36` design now requires authenticated callbacks with `Authorization: Bearer {progressToken}`, but the later `Sandbox-side work (upstream or fork)` section still only says to POST progress events to `progressUrl`. If an implementer follows that section literally, they will build an incompatible sandbox change that always gets `403 Invalid progress token` from the runner. This is exactly the kind of stale split-source contract that causes cross-repo integration churn.

Recommended fix: update the sandbox-side task list to include the required auth header and the full callback contract, for example "send `Authorization: Bearer {progressToken}` on every POST to `progressUrl`." If attempt numbers or any other required fields are part of the event schema, list them there too.

### 2. Medium (Functionality): the dev callback example uses `localhost`, which is not routable from inside the sandbox container
The callback-base examples say dev mode uses `http://localhost:8080`, but from inside a spawned container `localhost` refers to the sandbox container itself, not the workflow runner. That makes the example misleading and likely broken for exactly the local-development path the plan is trying to document.

Recommended fix: replace the dev example with a routable host that is valid from the sandbox runtime in local setups, or remove the hardcoded example and require an explicitly configured, sandbox-reachable `WORKFLOW_RUNNER_CALLBACK_URL`. The plan should state that the value must be reachable from inside the spawned sandbox, not from the operator's browser or host shell.

## Perspective Check
- Functionality: no new major runtime-lifecycle issues stood out after the update, but the dev callback example is still misleading enough to cause a broken local implementation.
- Quality: the plan is much more coherent now, but the sandbox-side section has drifted out of sync with the main `T36` contract.
- Security: the trust boundary is substantially improved, but the upstream sandbox task text still omits the required bearer-token behavior.

## Open Questions / Assumptions
- Is the sandbox-side work intended to happen in the same phase review scope, or is this document only responsible for the runner side and a separate sandbox review will define the exact event schema?

## Summary
Still not LGTM, but much closer. The first-round architecture/trust-boundary issues are largely addressed; the remaining work is to make the `T36` callback contract consistent across the whole document and fix the misleading local callback example.
