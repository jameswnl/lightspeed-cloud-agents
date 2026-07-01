# Review: Phase 1 implementation plan

## Findings

### 1. Blocker (Functionality, Quality): `T36`'s in-memory `ProgressStore` breaks the stateless runner contract and loses progress on replica/crash boundaries
The plan proposes an in-memory buffer in the workflow runner keyed by `workflow_id`, but the architecture doc says the runner is stateless and scales horizontally behind a load balancer. With the current design, progress events only exist in one runner process, SSE clients must land on that same replica, and a runner restart drops all in-flight progress. That makes the advertised streaming path non-functional in multi-replica deployments and fragile even in single-replica crash scenarios.

Recommended fix: either choose a side channel that is shared across replicas (for example Redis or another external event store/bus), or explicitly scope Phase 1 to a single-runner-only experiment and say it does not satisfy the stateless/multi-replica architecture yet. The plan should also say what survives runner restarts and what does not.

### 2. Major (Security): `T36` adds a sandbox-to-runner progress ingestion endpoint without an explicit trust or authentication mechanism
The proposed `POST /{workflow_id}/progress` endpoint accepts events from the sandbox, but the plan leaves open whether unknown workflow IDs should be accepted "since sandbox is trusted." That is not a safe trust boundary. In both Kubernetes and Podman, other workloads or local processes may be able to reach the runner unless the design defines how the runner authenticates the sender and binds a progress stream to the specific spawned sandbox instance.

Recommended fix: define an explicit callback-auth contract before implementation. For example, mint a per-step secret/token when spawning the sandbox, include it in the callback URL or header, and verify both the workflow identity and step identity on ingestion. The test plan should include rejection of missing/invalid tokens and spoofed workflow IDs.

### 3. Major (Functionality, Quality): `T36` uses `workflow_id` as the only progress key, which is not enough for parallel steps, retries, or idempotent cleanup
The parent implementation plan describes side-channel events as keyed by `(workflow_id, step_name)`, but this phase plan's `ProgressStore` and ingestion API are keyed only by `workflow_id`. That creates ambiguous routing when multiple steps run in parallel, when a step retries, or when cleanup/replay needs to distinguish one attempt's events from another's. A workflow-level cleanup could also erase progress for a still-running sibling step.

Recommended fix: define the event identity and lifecycle up front. At minimum, key streams by `workflow_id`, `step_name`, and attempt number (or another reconstructible per-execution identifier), and spell out when cleanup happens relative to completion, retry, timeout, and SSE disconnect/reconnect behavior.

### 4. Major (Quality, Security): `T1` quietly narrows the parent task from enforced tool scoping to request-body forwarding only
The phase plan says `T1` only verifies that `allowedTools` and `deniedTools` are forwarded to the sandbox and explicitly does not test enforcement. But the parent implementation plan and architecture gap analysis treat this work as part of closing the real per-step tool-filtering gap. As written, Phase 1 could be marked complete even if the sandbox ignores the new fields and the security behavior is still absent.

Recommended fix: either keep `T1` explicitly split into "runner forwarding" and "sandbox enforcement" sub-tasks with separate acceptance criteria, or retain an end-to-end verification that a denied tool is actually unavailable during workflow-driven execution. If sandbox support is still undecided, the plan should say Phase 1 only delivers a partial prerequisite and does not close the security gap by itself.

### 5. Medium (Functionality): `T36` assumes a routable `progressUrl` but does not define who owns callback URL derivation in Kubernetes vs Podman
The plan recommends direct runner callbacks and shows `request_body["progressUrl"] = f"http://{runner_host}:{runner_port}/..."`, but it never defines where `runner_host` and `runner_port` come from, which address is valid from inside the sandbox, or how this differs between Kubernetes Services and Podman networking. Without that contract, implementers can build incompatible per-environment guesses.

Recommended fix: add one source of truth for callback addressing, such as a configured runner callback base URL per deployment target, and state exactly how the activity derives the per-step progress endpoint from it. The tests should cover both supported deployment modes or explicitly defer one.

## Perspective Check
- Functionality: major gaps remain in `T36` runtime semantics, especially statelessness, callback reachability, and stream identity across retries/parallelism.
- Quality: the phase plan is not fully internally aligned with the parent plan; `T1` under-specifies completion, and `T36` leaves core lifecycle contracts undefined.
- Security: the new progress-ingestion trust boundary is not yet explicit enough, and `T1` should not be treated as closing the tool-scoping gap without end-to-end enforcement.

## Open Questions / Assumptions
- Is Phase 1 supposed to preserve the stateless multi-replica runner architecture, or is `T36` intentionally allowed to be a single-runner stepping stone?
- Does the sandbox contract already support authenticated progress callbacks, or would that be a new upstream change alongside `progressUrl`?
- Is `T1` intended to be "forward config only" for this phase, or to materially reduce the current per-step tool-filtering security gap?

## Summary
Not LGTM. `T3` and `T22` look straightforward, but `T36` still lacks an implementable, architecture-consistent lifecycle/trust model, and `T1` currently weakens the parent task's security intent by treating forwarding alone as sufficient completion.
