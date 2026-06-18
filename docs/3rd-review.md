# Review: `cloud-agents.md`

## Findings

### 1. Blocker: Phase 1b cross-pod scenarios do not work if each pod owns its own simulated cluster state

`cloud-agents.md` says the monitoring pod will detect issues and dispatch the diagnostic pod across HTTP, and Phase 1b acceptance requires the PoC scenarios to work end-to-end across containers. But `phase-1a-tasks.md` explicitly says each agent pod gets its own in-memory simulated state on process start.

That means the monitoring agent and diagnostic agent are no longer looking at the same world, so the handoff is not actually validating multi-agent collaboration.

```text
lightspeed-stack deployment
├── core pod
├── agent pod: diagnostic-agent
└── agent pod: monitoring-agent
```

And separately:

```text
Each agent pod has its own process = its own state.
```

#### Why this matters

If monitoring sees a failing simulated host and then dispatches diagnostic over HTTP, the diagnostic agent may inspect a different fresh in-memory state and not see the same failure. That undermines the main Phase 1b claim.

#### Recommendation

Pick one and state it clearly:

- **Option A:** Keep simulated state only for Phase 1a, and make Phase 1b switch to real cluster APIs.
- **Option B:** If Phase 1b still uses simulation, introduce a shared state service/backend so both agents inspect the same state.

Right now the docs imply shared collaboration but define isolated state.

### 2. Major: the lifecycle section assumes a controller/orchestrator, but the task plan only budgets for static manifests

The design says the core pod creates Deployments from config and watches Pod events to build the registry. That is substantial control-plane behavior.

But the Phase 1a task plan only includes:

- config parsing
- registry object
- container image
- static Kind/Podman manifests

Those are not the same scope.

#### Conflicting statements

The design describes:

- core pod creates Deployments from agent config
- core pod watches Pod events
- core pod builds registry from service discovery

But the task plan describes:

- `RemoteAgentClient`
- config validation
- static deployment manifests
- manual setup scripts

#### Why this matters

This creates ambiguity about whether Phase 1 is:

1. a **static deployment model** with pre-created pods, or
2. a **real orchestration model** where core manages agent lifecycle.

Those are very different implementations and estimates.

#### Recommendation

For Phase 1, choose one explicit model:

- **Static model:** agents are predeployed and configured; core only calls them.
- **Managed model:** core creates and tracks agent deployments dynamically.

Given the current task list, the real scope looks like the **static model**. If that is the intent, rewrite the lifecycle section to match it.

### 3. Major: agent discovery has no single source of truth

The config model requires explicit `endpoint` URLs. The YAML example says those URLs may be auto-discovered. The lifecycle section says the registry is populated from K8s Service discovery or compose config.

That is too many discovery models for Phase 1.

#### Why this matters

This affects:

- validation
- rollout
- debugging
- failure handling
- Podman vs OCP parity

If an agent is both configured and discovered, it becomes unclear:

- which source wins on conflict
- what is required vs optional
- what the registry actually trusts

#### Recommendation

Pick one for Phase 1:

- **Config-driven:** `endpoint` is required, config is authoritative.
- **Discovery-driven:** config defines identity only, platform resolves endpoint.

I would strongly recommend **config-driven** for Phase 1a/1b because it is simpler and matches the current task breakdown.

### 4. Major: the `/v1/run` response envelope is too weak for the multi-agent roadmap

The proposed `AgentRunResponse` has:

- `output: dict`
- `usage: dict`
- `agent_name`
- `success`
- `error`

That is enough for a single diagnostic agent spike, but weak for a roadmap that adds:

- monitoring agents
- conversational delegation
- later user-defined agents
- later workflow-driven agents

#### Why this matters

Once more than one agent type can respond via the same contract, callers need to know what schema they received.

Right now the response assumes the caller already knows the output type. That does not scale well.

#### Recommendation

Add at least:

- `output_type`
- `schema_version`

For example:

```json
{
  "agent_name": "diagnostic-agent",
  "success": true,
  "output_type": "DiagnosticReport",
  "schema_version": "v1",
  "output": {}
}
```

That keeps the contract stable as more agent types arrive.

## Open Questions / Assumptions

1. Is Phase 1 intended to support only **static pre-deployed agents**, or should the core pod really create/manage Deployments dynamically?
2. Is simulated cluster state only a **Phase 1a harness**, with Phase 1b switching to real cluster APIs?
3. Should agent registry be **config-driven** or **discovery-driven** in Phase 1?
4. Is `/v1/run` meant to be a generic agent protocol from the start, or just a diagnostic-agent-specific contract for Phase 1a?

## Summary

The overall direction is strong. The document clearly explains why cloud agents matter and why this is a better differentiator than being a `/responses` proxy.

The biggest weakness is not the vision, but the transition plan:

- the Phase 1 narrative assumes true cross-pod collaboration
- the detailed implementation still behaves more like a single-agent spike plus static deployment
- the state/discovery/lifecycle model is not yet internally consistent

If those boundary lines are tightened, the plan will be much more credible and easier to execute.

## Suggested Rewrite Priorities

If you revise `cloud-agents.md`, I would fix these in order:

1. Clarify whether Phase 1b uses **real cluster state** or a **shared simulated state**.
2. Rewrite lifecycle management to match the actual scoped implementation.
3. Pick one source of truth for agent discovery.
4. Strengthen the `/v1/run` envelope for future agent types.