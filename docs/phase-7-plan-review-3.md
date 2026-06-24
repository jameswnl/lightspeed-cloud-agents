# Review: phase-7-tasks.md

## Findings

### 1. Medium: Task 3 still mixes two incompatible Kubernetes token propagation models
The updated auth section correctly says Kubernetes production should use projected ServiceAccount tokens with TokenReview validation, but the concrete fix bullets above it still say the spawner passes `AGENT_API_TOKEN` env vars to ephemeral pods and the workflow runner reads the token from env. That matches the Podman/shared-secret model, not the Kubernetes production model the doc now claims to use.

Why it matters:
- implementers still do not have one clear source of truth for how K8s credentials reach the runner and spawned Jobs
- "Secret ref + env var" and "projected SA token volume" imply different spawner config, runtime wiring, and test coverage
- this is security-sensitive plumbing, so ambiguity here is more than wording drift

Recommended fix:
- split the concrete fix bullets by deployment target, just like the token model section does
- for Kubernetes, describe projected token volume wiring and how the runner / client read the token
- reserve `AGENT_API_TOKEN` env propagation for Podman/shared-secret mode only

## Perspective Check
- Functionality: covered. The cleanup and fail-closed approval concerns from the previous pass were resolved at the plan level.
- Quality: remaining gaps. Task 3 still mixes deployment-specific implementation details in a way that could send implementation down the wrong path.
- Security: remaining gaps. The high-level K8s auth model is now sound, but the concrete propagation mechanism is still ambiguous.

## Open Questions / Assumptions
- Should the runner also use a projected ServiceAccount token in Kubernetes mode, or only spawned Jobs?
- Is the intent for `auth_mode` to switch both credential source and validation path, or only the outbound header value?

## Summary
Very close. The major issues from the earlier reviews look resolved. The remaining gap is a narrower Task 3 consistency problem: the high-level Kubernetes auth model now says projected SA tokens, but the implementation bullets still describe shared-secret env propagation.
