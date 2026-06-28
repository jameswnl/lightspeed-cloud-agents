# Review: `phase-1-tasks.md`

## Findings

### 1. Blocker (Functionality): approval timeout is tested, but the plan never defines how it is configured or enforced

The task list promises approval timeout semantics and a `denied` outcome, but it never adds a timeout field to the workflow definition, never wires the architecture's approval config into the Temporal workflow, and never defines what event/status transition should be emitted on timeout. That leaves a core user-visible behavior unimplementable from the plan alone.

Recommended fix:
- add an explicit timeout contract now
- define where the timeout comes from (`step.approval_timeout_seconds` or config default)
- define the exact Temporal mechanism used to enforce it
- define the resulting status and event semantics exposed by query/SSE

### 2. Blocker (Functionality): `approvedOption` is derived from analysis output, not from the approval decision

The current plan says `approvedOption` is built from the analysis step output by role. That silently assumes the first analysis option is the approved one. If a human approves option 2 or 3, the execution step can still receive option 1 in context. This breaks the core diagnose/approve/execute path and also makes retries and escalation harder to reconstruct because the chosen option is not persisted as a stable identifier.

Recommended fix:
- require analysis options to carry a stable `id`
- require the approval step to emit the selected option id
- build `approvedOption` from the approval output plus the referenced analysis option, not from role lookup alone

### 3. Major (Functionality): crash-boundary cleanup is not defined, so retries can orphan sandbox workloads

The plan relies on `finally: spawner.destroy(pod_name)` plus an attempt-derived name. That is not enough across worker crashes, hard kills, or mid-activity timeouts. If the worker dies after spawn but before cleanup, the next retry may run with a different attempt number and no deterministic way to locate the previous pod/container. That weakens the architecture's durability and recovery story and risks leaked sandboxes or duplicate side effects.

Recommended fix:
- define one recovery-safe cleanup mechanism now
- either persist a stable execution identity, or require cleanup by workflow/step labels, or define a sweeper/reconciler path
- add explicit tests for crash-after-spawn and cancel-while-running

### 4. Major (Quality): the public workflow API has two competing sources of truth

The phase plan defines `/v2/workflows/*`, while the architecture and trigger design describe `/v1/workflows/*`. This is more than wording drift: it affects router registration, auth reuse, trigger integrations, and later phases that build on this surface.

Recommended fix:
- choose the canonical API version now
- if `v2` is intentional, update the architecture and trigger docs to match and explain compatibility expectations
- otherwise keep phase 1 on `/v1/workflows/*`

### 5. Major (Security): the provider env var and credential model is inconsistent, especially for Podman

The documents currently mix `LIGHTSPEED_AGENT_PROVIDER` and `LIGHTSPEED_PROVIDER`, and they shift between `SecretKeyRef`, mounted credential files, and Podman host-env propagation without one explicit authoritative contract. That leaves implementers without a single source of truth and makes the Podman trust boundary too implicit for a phase that claims dual-target support.

Recommended fix:
- define one authoritative sandbox env and credential contract
- have the task list reference that contract directly
- explicitly state the Podman credential mechanism and intended trust boundary, even if it is only an accepted phase-1 compromise

### 6. Medium (Quality): the E2E plan proves the happy path, but not the riskiest state transitions

The end-to-end plan checks successful execution and approval flow, but the most important reliability claims here are elsewhere: HTTP 502 retry behavior, retry exhaustion to escalation, cancel/cleanup, pre-deployed dispatch, and old-Kubernetes skills-volume fallback. Without at least one unhappy-path E2E, the hardest integration claims remain unproven.

Recommended fix:
- add one failure-path E2E in phase 1
- preferably `502 -> retry -> eventual success` or `retry exhaustion -> escalated terminal state -> cleanup verified`

## Perspective Check
- Functionality: remaining gaps in approval timeout semantics, approved-option selection, and crash-safe cleanup.
- Quality: remaining gaps in API version consistency, env-var contract consistency, and failure-path test coverage.
- Security: partially covered, but the Podman credential and trust-boundary story is still too implicit.

## Open Questions / Assumptions

- I assumed phase 1 is meant to be implementation-ready, not just a rough backlog.
- I assumed approval may select among multiple remediation options rather than only approve/deny a single proposal.
- I assumed Podman is intended as a real supported target in this phase, not only a local-dev shortcut.

## Summary

The plan is close, but it is not fully implementation-ready yet. The main gaps are contract-level: approval timeout behavior, approved option reconstruction, cleanup across crash boundaries, and conflicting API/env contracts. Tightening those before implementation will reduce both execution risk and test ambiguity.
