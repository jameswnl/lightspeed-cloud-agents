# Review: `phase-2-tasks.md`

## Findings

### 1. Major (Security): Task 2 still does not explicitly require the Podman-side advisory enforcement described in the contract

The contract now says advisory mode is enforced differently on the two deployment targets: Kubernetes uses a read-only `service_account`, while Podman uses `--read-only` plus no host mounts. But Task 2's implementation text only says to pass the advisory flag and set a read-only `service_account` when `advisory=true`. That is sufficient for Kubernetes and irrelevant for Podman. Because Podman is still a first-class target in this phase, the task text can be implemented "correctly" for K8s while still omitting the Podman enforcement path the contract promises.

Recommended fix:
- extend Task 2 so it explicitly requires the Podman spawner to add the advisory-specific `--read-only` / no-host-mount behavior
- add a focused test for the Podman advisory path, not just the Kubernetes `service_account` path

### 2. Medium (Quality): Task 4 still uses stale "at-most-once" wording even though the contract now allows duplicates

The Notification Delivery Contract now consistently says "best-effort with possible duplicates," which resolves the earlier contradiction. But Task 4 still says "At-most-once delivery, no retry." That leaves the doc with two different behavioral descriptions again, just in a smaller form. The task text should match the contract text so implementation and tests target the same semantics.

Recommended fix:
- update Task 4 to use the same wording as the contract
- keep `correlation_id` and `maximum_attempts=1`, but describe the behavior as best-effort with a crash-window duplicate possibility

### 3. Medium (Quality): Task 12 still implies sandbox-request permission propagation even though that part was deferred

The Permissions Scope Contract now narrows Phase 2 to `service_account` passthrough and timeout override, with request-level tool filtering explicitly deferred. But Task 12 still says the integration tests cover "permissions in request," which sounds like the older sandbox-request propagation design that phase 2 intentionally backed away from. This is a smaller wording issue, but it reopens ambiguity about what the integration suite is supposed to prove.

Recommended fix:
- rewrite the Task 12 permission test bullet to match the narrowed contract
- for example, say the integration tests cover `service_account` passthrough / advisory enforcement rather than "permissions in request"

## Perspective Check
- Functionality: no new major functionality gaps found in this pass.
- Quality: remaining gaps are stale task wording that no longer matches the refined contracts.
- Security: the remaining meaningful gap is that Podman advisory enforcement is specified in the contract but not yet made explicit in the task implementation/test path.

## Open Questions / Assumptions

- I assumed the Podman advisory path is intended to be implemented in this phase because the contract already names it as part of the enforcement model.
- I assumed "permissions in request" in Task 12 was leftover wording from the earlier tool-filtering approach rather than a deliberate re-expansion of scope.

## Summary

The doc is close, but I would not write `LGTM` yet. The remaining work is mostly cleanup of task/contract alignment, except for one still-meaningful security issue: the Task 2 implementation path needs to explicitly include the Podman advisory enforcement that the contract now promises.
