# Productization Roadmap Review

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-06-29
**Status**: All 6 issues resolved. Superseded by `productization-roadmap.md` (updated) and `prod/implementation-plan.md`.

## Verdict: Solid. Six issues to address. *(All resolved — see status above)*

The roadmap is thorough, correctly sourced from the code-level gap analysis, and well-structured with clear P0/P1/Backlog separation. The "where" + "what to build" format makes each item actionable.

## Issues

### 1. Audit logging and JSON logging are two P0 items that should be one

"Structured audit logging" and "Structured JSON logging" overlap. Audit logging is security-relevant events in JSON format — it's a subset of structured logging, not a separate concern. Merging them into one task avoids duplicate logging infrastructure:

- **One task**: Switch to structured JSON logging (`structlog` or `python-json-logger`), then add a dedicated `AuditEvent` model emitted on security-relevant actions (workflow start, approval, sandbox spawn/destroy, escalation). Same JSON pipeline, same log stream, different event schemas.

### 2. Rate limiting may be P1, not P0

The spawner already has `MAX_SPAWNED_PODS=10` and the Temporal worker has `max_concurrent_activities=10`. For initial production with a single team, these caps are sufficient — a caller can't trigger a pod storm because the worker won't execute more than 10 activities concurrently.

Per-user rate limiting matters when multiple teams share the deployment. That's a multi-tenancy concern — P1, not P0. Unless there's a specific compliance requirement driving this.

### 3. Credential volume mount and MCP injection: are these done in Phase 3 or not?

The roadmap header says "Phases 1-3 complete" but these two items are listed as P0 gaps. The Phase 3 tasks doc (`phase-3-tasks.md`) includes them. Three possibilities:

- Phase 3 was completed **without** these items (they were added to the tasks doc after Phase 3 shipped)
- Phase 3 included them but they're **not yet verified**
- The header is wrong and Phase 3 is **not fully complete**

Clarify which. If they're not done, they're the highest-priority P0 items since they block Vertex/Bedrock providers and MCP extensibility.

### 4. Missing: upstream sandbox PRs (executionResult + HTTP 502)

Phase 3 tasks had:
- **T12**: Upstream PR — `executionResult` context formatting in sandbox's `_format_context_prefix()`
- **T13**: Upstream PR — HTTP 502 for infrastructure errors

Neither appears in the productization roadmap. Are they:
- Already submitted and merged?
- Submitted but pending review?
- Dropped?

If pending, they should be in P0 — the HTTP 502 PR directly affects the retry model (without it, the activity falls back to string-matching heuristics for error classification).

### 5. Missing: workflow definition validation

Phase 3 tasks had **T19**: Validate YAML at submission time (circular conditions, undefined step references, duplicate output_key). Not in the roadmap. This is a P1 item at minimum — invalid workflow definitions currently fail at runtime deep in the Temporal workflow, producing confusing errors.

### 6. API idempotency keys is already solved — move to docs

The P1 item says: "Temporal already handles this — `workflow_id` in `WorkflowInput` is the idempotency key." If it's already solved, it's not a build task. Either:
- Remove it from the roadmap entirely, or
- Move to Documentation Debt: "Document that `workflow_id` serves as an idempotency key. Return 409 for duplicate submissions."
