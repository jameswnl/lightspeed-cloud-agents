# Review: Phase 1b Plan (Updated)

## Findings

### 1. Major: the plan still contradicts itself on per-tool metrics, which will create implementation drift

The updated plan resolves the earlier instrumentation concern by deferring per-tool metrics to Phase 2 and scoping Phase 1b to per-run metrics only. However, the main observability section still says Phase 1b adds `agent_tool_calls_total{agent_name, tool_name}`.

#### Why this matters

This leaves two competing sources of truth in the same document:

- one says per-tool metrics are deferred
- another still lists them as part of Phase 1b

That is no longer an architectural blocker, but it is still a real planning defect because an implementer could reasonably build either version.

#### Recommendation

Remove the stale per-tool metric from the observability overview so the plan says exactly one thing:

- Phase 1b: per-run metrics only
- Phase 2: per-tool metrics once the instrumentation hook exists

### 2. Major: the trust-boundary language resolves the cluster case, but the Podman case is still underspecified

The updated plan now has a clear Phase 1b trust-boundary section. That resolves the earlier concern for Kind/OCP-style deployment: the endpoints are explicitly internal-only, unauthenticated, and acceptable only for dev/test.

But the same plan still lists Podman as a first-class deployment target, and the containment rules are described in Kubernetes terms (`ClusterIP`, manifests, no Ingress). The Podman story is not stated with the same precision.

#### Why this matters

The document now clearly answers:

- what “internal-only” means in Kubernetes
- what the dev/test limitation is
- what assumptions exist around unauthenticated polling and metrics

But it still does not clearly answer:

- whether Podman uses host port bindings
- whether Podman E2E requires exposing the agent endpoints locally
- how that fits the “internal-only” rule

#### Recommendation

Add a small Podman-specific note under the trust-boundary or deployment section, for example:

- Podman compose is dev-only
- host port exposure is allowed for local testing only
- do not expose the compose stack beyond the developer machine/network

That makes the security posture symmetrical across both declared deployment targets.

## What Improved

The updated plan is materially better. The major issues from the prior review are now addressed directly in the document:

- repeated monitoring redispatch now has a concrete answer: local state mutation after successful diagnostic completion
- `/livez` now distinguishes loop-driven and request-driven agents
- E2E dispatch verification now has a defined observation path with `dispatched_run_ids`
- the trust boundary is explicit instead of implied
- correlation ID validation is now concrete and security-aware

Those changes remove the earlier architectural blockers and make the plan far more executable.

## Summary

The plan is now in good shape overall. I no longer see the earlier design blockers as unresolved.

The remaining work is mostly cleanup and consistency:

1. remove the stale per-tool metrics mention from the observability section
2. make the Podman security story as explicit as the Kind/OCP story

If those two items are tightened, I would consider the plan review-ready.
