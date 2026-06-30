# Review: `implementation-plan.md` and `productization-roadmap-review.md`

## Findings

### 1. Blocker (Functionality): graceful shutdown still has no crash-proof cleanup mechanism
`implementation-plan.md` includes a graceful shutdown task, but the concrete work described there only wires SIGTERM into FastAPI lifespan and relies on the Temporal worker's existing context-manager shutdown behavior.

That does not satisfy the stronger behavior promised by the roadmap, which says shutdown should stop new work, wait for in-flight activities, and clean up remaining sandbox pods or containers. If the worker dies after spawning a sandbox but before the activity `finally` block runs, this plan does not define any durable source of truth for active sandboxes or any startup reconciliation sweep that can find and destroy leftovers after restart.

**Why it matters:** the current acceptance criteria claim "no orphaned sandbox containers after shutdown," but the plan does not yet define an implementable recovery path across crash boundaries.

**Recommended fix:** split this into two explicit contracts:
- graceful SIGTERM drain for healthy shutdowns
- crash/restart reconciliation for already-orphaned sandboxes

Then define how active sandboxes are identified after restart, for example via durable workflow metadata plus a label-based reconciliation pass keyed by `workflow_id`, `step_name`, and `attempt`.

### 2. Major (Functionality): MCP configuration still lacks a single authoritative schema
The implementation plan says to add `mcp_servers` to "`WorkflowInput` or step config," which leaves the contract ambiguous at the exact boundary where API validation, workflow schema, activity logic, and pod env-var serialization all need to agree.

This leaves several runtime questions unanswered:
- Is MCP configuration workflow-global or step-local?
- If both exist, what is the inheritance or override rule?
- Are secret mounts created once per workflow or once per step?
- What exact JSON shape is written into `LIGHTSPEED_MCP_SERVERS`?

**Why it matters:** this creates silent cross-task coupling. Different tasks can each implement a reasonable interpretation and still end up incompatible.

**Recommended fix:** choose one source of truth for MCP configuration, define inheritance rules explicitly, and include one worked example showing YAML input, resulting env var JSON, and mounted secret paths.

### 3. Major (Security): MCP secret mounting has no explicit authorization boundary
The plan says secret-backed MCP auth headers should be mounted into sandbox pods, but neither reviewed doc defines who is allowed to reference those secrets or what constrains the allowed secret set.

As written, this risks turning workflow configuration into an arbitrary secret-selection surface unless there is an unstated admission rule elsewhere. That is too implicit for a productionization-phase task, especially since the roadmap already frames this work in compliance and audit terms.

**Why it matters:** the plan introduces a new credential-loading path into execution pods without defining the trust boundary for who controls it.

**Recommended fix:** make the policy explicit. For example:
- only allow MCP servers from a deployment-managed allowlist
- forbid arbitrary user-supplied `secretRef`
- require secret references to come from pre-registered server definitions in the same namespace
- document how the sandbox reads those mounted credentials without broadening secret access

### 4. Major (Quality): credential secret naming drifts from the earlier schema contract
`phase-3-tasks.md` explicitly keeps `ProviderConfig` as `{name, model, credentials_secret}`, but `implementation-plan.md` introduces `credentials_secret_name`.

That is a silent schema rename with no compatibility note.

**Why it matters:** this creates avoidable migration and documentation risk. Existing workflow definitions, validation, or companion docs may still use `credentials_secret`, and this review found no statement that both names are accepted or that a migration is planned.

**Recommended fix:** keep `credentials_secret` as the canonical field name, or explicitly define backward compatibility and migration behavior if `credentials_secret_name` is intentional.

### 5. Medium (Quality): the implementation plan says "P0 only" but includes a roadmap P1 item
The implementation plan says its scope is "P0 tasks for production readiness + ARCHITECTURE.md rewrite," but it also includes per-workflow `model_provider`, which the roadmap places in P1.

**Why it matters:** this weakens the phase boundary and makes the prioritization harder to trust. Either the roadmap is wrong about launch scope, or the implementation plan has quietly expanded beyond its stated remit.

**Recommended fix:** either promote per-workflow `model_provider` in `productization-roadmap.md` with rationale, or remove it from this implementation plan so the documents stay aligned.

### 6. Medium (Quality): `productization-roadmap-review.md` now reads as stale rather than current
Several issues raised in `productization-roadmap-review.md` have already been incorporated into the roadmap or implementation plan:
- logging and audit were merged
- rate limiting moved to P1
- upstream PRs were added
- validation is now marked done
- idempotency moved toward docs plus API behavior

The review doc still reads like a live unresolved issue list rather than a historical review artifact with follow-up status.

**Why it matters:** readers can no longer tell which issues are still open versus already resolved, which makes the document misleading as an input to current planning.

**Recommended fix:** add a short status note under each issue or mark the review as superseded by the current roadmap and implementation plan.

## Perspective Check
- Functionality: covered; remaining gaps are the crash-recovery cleanup path and the unresolved MCP runtime contract.
- Quality: covered; remaining gaps are schema drift, scope drift, and stale status in the prior roadmap review artifact.
- Security: covered; the main remaining gap is the missing trust boundary for MCP secret references. No other major security issues stood out in these two docs.

## Open Questions / Assumptions

- Are workflow authors in the intended production deployment fully trusted operators, or merely authenticated users?
- Was `credentials_secret_name` an intentional schema rename, or wording drift from the earlier phase docs?
- Should per-workflow `model_provider` be part of launch scope, or is it acceptable to keep it as roadmap P1?

## Summary

The planning set is directionally solid and improved by the earlier roadmap review, but it is not yet LGTM. The biggest remaining issues are the missing crash-recovery mechanism behind the shutdown claim, the lack of a single MCP contract, and the missing trust boundary for MCP secret mounting.
