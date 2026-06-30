# Review: `phase-2-tasks.md`

## Findings

### 1. Major (Quality): the notification contract still contradicts itself about duplicate delivery

The new notification section says delivery is "at-most-once, best-effort" with `maximum_attempts=1`, but it also says a worker crash after sending can cause the notification to be re-sent and that duplicates are acceptable. Those two statements do not describe the same contract. If duplicates are possible in a known crash window, the design is not truly at-most-once, and implementers/testers will not know which behavior to treat as correct.

Recommended fix:
- choose one contract and name it accurately
- if duplicates in the crash window are acceptable, describe the behavior as best-effort with possible duplicates and keep the `correlation_id` requirement
- if true at-most-once delivery is required, add a persisted send marker or equivalent idempotency mechanism and test it

### 2. Medium (Quality): Task 7 still contains stale Podman skills-loading text that conflicts with the new contract

The contracts section now clearly says the Podman mechanism is named-volume extraction via `podman volume create` plus `podman run --rm -v`, and that this replaces the earlier `podman cp` approach. But Task 7 still says `Podman: podman cp`, which leaves two competing sources of truth in the same document.

Recommended fix:
- update Task 7 to match the Skills Image Loading Contract
- keep the tests aligned with the actual Podman volume/extraction path rather than the superseded `podman cp` wording

## Perspective Check
- Functionality: no major new functionality gaps found in this round.
- Quality: remaining gaps in notification semantics and stale Podman skills-loading text.
- Security: no major unresolved security issues found in this round.

## Open Questions / Assumptions

- I assumed the crash-window duplicate noted in the notification contract is intentional rather than accidental wording.
- I assumed the named-volume extraction path is the intended final Podman design because the contract section explicitly says it replaces `podman cp`.

## Summary

This revision resolves the earlier major contract gaps around advisory enforcement, secret-safe config references, durable escalation artifacts, and deferring unsupported tool filtering. The remaining issues are smaller but still worth fixing before LGTM: one semantic contradiction in notification delivery guarantees and one stale Task 7 line that conflicts with the updated Podman skills-loading contract.
