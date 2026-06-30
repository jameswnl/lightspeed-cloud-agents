# Review: `implementation-plan.md` and `productization-roadmap.md`

## Findings

### 1. Blocker (Functionality): the MCP secret-header resolution path cannot work as written
`implementation-plan.md` now gives MCP a concrete schema and trust boundary, which resolves most of the earlier ambiguity. But the execution mechanism in T2 still has an internal contradiction:

- the plan says MCP auth Secrets are mounted into the **sandbox pod**
- the plan also says `temporal_activities.py` will "read mounted Secret files to resolve header values in the serialized JSON"

Those two things cannot both be true unless the same Secret files are also mounted into the workflow-runner process that executes `temporal_activities.py`, which the plan does not describe. As written, the activity cannot read files that only exist inside the spawned sandbox container.

This is also a security footgun: if the intended fix is to read the Secret value in the workflow-runner and inline it into `LIGHTSPEED_MCP_SERVERS`, the plan would be moving sensitive header material into a plain env var instead of preserving the file-based secret boundary it just introduced.

**Why it matters:** T2 currently promises a mechanism that is not implementable as specified, and the most obvious workaround weakens the secret-handling model.

**Recommended fix:** choose one of these contracts explicitly:
- keep `LIGHTSPEED_MCP_SERVERS` free of secret values and encode file references that the sandbox resolves locally from its mounted Secret paths, or
- mount the Secret into the workflow-runner too and explicitly accept that the runner materializes the header value before spawn

The first option is the safer design. Whichever contract you choose, show one concrete before/after example for both the env var JSON and the mounted file path that the sandbox consumes.

### 2. Medium (Quality): the roadmap and implementation plan still disagree on whether credential mounting needs a schema change
The updated implementation plan correctly says `ProviderConfig.credentials_secret` already exists and no schema change is needed for T1. But `productization-roadmap.md` still says the credential mount requires "a new field on `ProviderConfig`."

**Why it matters:** this is now a two-sources-of-truth problem. A reader using the roadmap as the parent planning document will come away with a different migration story than a reader using the implementation plan.

**Recommended fix:** update `productization-roadmap.md` so it matches the implementation plan: credential volume mounting reuses the existing `credentials_secret` field and does not require a schema migration.

## Perspective Check
- Functionality: covered; the remaining blocker is the impossible MCP secret-resolution mechanism.
- Quality: covered; the remaining gap is the schema-migration contradiction between the roadmap and implementation plan.
- Security: covered; the main remaining risk is accidentally turning secret-backed MCP headers into plain env-var data during resolution.

## Open Questions / Assumptions

- Does `lightspeed-agentic-sandbox` already support header values supplied via mounted-file references, or would that require a small upstream or local contract extension?
- Is the roadmap intended to be updated in lockstep with the implementation plan whenever a lower-level contract changes?

## Summary

The updated plan set is much closer. The previous shutdown, MCP-authority, secret-boundary, and P0/P1 scope issues are mostly resolved. It is not `LGTM` yet because T2 still describes an MCP secret flow that cannot execute as written without either an extra mount point or a weaker secret model, and the roadmap still has one stale schema-migration claim.
