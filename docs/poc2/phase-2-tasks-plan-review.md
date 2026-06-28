# Review: `phase-2-tasks.md`

## Findings

### 1. Blocker (Functionality): Task 3 assumes request-level tool filtering exists, but the companion design still treats that sandbox contract as future work

Task 3 says the activity should pass `allowed_tools` and `denied_tools` in the sandbox request and test that those filters flow through. But the companion Temporal sandbox design still describes request-level tool configuration as a "nice to have" adaptation rather than an established runtime contract, with the current sandbox default tool set still hardcoded. That means one of the core phase-2 promises, per-step permissions wiring, has no fully defined implementation target yet.

Recommended fix:
- either promote request-level tool filtering into an explicit required sandbox contract for this phase, including request schema and enforcement point
- or narrow Task 3 to only the parts that are already implementable now (`service_account`, timeout override) and move tool filtering behind a separate prerequisite task
- update the test list so it only asserts behavior that the design actually guarantees

### 2. Major (Security): advisory mode skips approval, but the plan never defines a hard read-only enforcement path

Task 2 wires `advisory` through prompt annotation, approval skipping, output marking, and the sandbox request. That is not enough to guarantee advisory-only behavior. As written, an "advisory" run can still execute with write-capable tools or a write-capable ServiceAccount unless the permissions path is coupled explicitly. This is a trust-boundary problem, not just a wording gap: the plan changes operator expectations ("advisory") without defining the mechanism that prevents writes.

Recommended fix:
- define the authoritative enforcement path for advisory mode now
- if advisory relies on tool filtering, make Task 2 depend explicitly on the sandbox contract from Task 3
- if advisory relies on RBAC instead, require read-only `service_account` semantics and say how Podman is constrained
- add at least one integration test that proves advisory mode cannot take a write path, not just that it skips approval

### 3. Major (Security): `notifier_config` and `escalation_config` on `WorkflowInput` risk pushing secret-bearing delivery config into Temporal payloads

Tasks 4 and 5 add `notifier_config` and `escalation_config` directly to `WorkflowInput`. The surrounding Temporal design explicitly treats secrets as runtime-only data that should not flow through workflow inputs or activity payloads. If those configs contain webhook URLs, auth headers, tokens, or destination credentials, the plan reintroduces exactly the kind of payload-level secret leakage the architecture tries to avoid.

Recommended fix:
- define these fields as references to server-side config, not raw secret-bearing config objects
- keep secret resolution in worker/activity runtime code, not in workflow input payloads
- say explicitly which pieces of notifier/escalation configuration are safe to serialize and which must stay in env/config storage

### 4. Major (Functionality): escalation delivery is marked non-fatal, but the plan does not preserve a durable source of truth for the handoff artifact

Task 5 says `EscalationPackager` delivery failure is non-fatal, which is reasonable, but the task text never says where the escalation package still lives if delivery fails. The companion architecture promises a durable handoff artifact that can be retrieved later for a CLI resume flow. Without an explicit local source of truth, a failed webhook/package delivery can leave the workflow "escalated" but with no guaranteed handoff payload for the human to retrieve.

Recommended fix:
- require `build_escalation_activity` to continue returning/storing the escalation artifact in workflow state or a retrievable API-backed store
- treat external delivery as a secondary distribution path, not the only copy
- add a test that packager delivery fails but the escalation payload remains available for later retrieval

### 5. Major (Quality): approval notification is "fire-and-forget," but the plan never defines delivery semantics across retry and crash boundaries

Task 4 says the workflow dispatches notification on pause using a fire-and-forget activity. That leaves the worker with no stated contract for duplicates, retries, or recovery after partial failure. A pause notification can be dropped silently, or it can be delivered multiple times after retry/replay, and the current test list does not force the plan to choose which behavior is acceptable.

Recommended fix:
- define whether notification delivery is at-most-once or at-least-once
- if duplicates are acceptable, say so explicitly and include a stable correlation key for receivers
- if duplicates are not acceptable, persist a notification idempotency key and require notifiers to use it
- extend tests beyond "called" to cover retry or duplicate-safe behavior

### 6. Medium (Quality): Task 7 changes the Podman skills-loading mechanism without reconciling it with the prior design

Task 7 switches Podman skills support to `podman cp`, while the earlier phase/design material described image-volume style mounting as the main path. The new mechanism may be workable, but the plan does not explain whether this is an intentional design change, how retry/cleanup interacts with copied files, or what behavior parity is expected between Podman and Kubernetes.

Recommended fix:
- state whether `podman cp` is replacing the earlier Podman image-mount approach or only a fallback
- define cleanup expectations for copied skills content after crash/retry
- align the architecture/task docs so implementers do not inherit two competing delivery mechanisms

## Perspective Check
- Functionality: remaining gaps in per-step tool-permission implementability and durable escalation handoff behavior.
- Quality: remaining gaps in notification delivery semantics and skills-loading contract consistency.
- Security: remaining gaps in advisory-mode enforcement and in keeping secret-bearing delivery config out of Temporal payloads.

## Open Questions / Assumptions

- I assumed phase 2 is meant to be implementation-ready and is not intentionally leaving sandbox request-contract changes unspecified.
- I assumed "advisory" is meant to be operationally read-only, not merely a UX label on otherwise write-capable runs.
- I assumed notifier and escalation configuration may include secret-bearing destination details unless the plan says otherwise.

## Summary

The phase is close to a workable final wiring plan, but several of the highest-risk behaviors still depend on contracts that are either missing or only implied: request-level tool filtering, advisory enforcement, secret-safe delivery config, durable escalation artifacts, and notification semantics across retries. Tightening those now will make the implementation and test plan converge on one predictable runtime model.
