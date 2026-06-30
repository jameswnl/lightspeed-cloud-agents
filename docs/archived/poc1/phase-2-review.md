# Review: `phase-2-template-design.md`

## Findings

### 1. Blocker: the schema and the output-type resolution design contradict each other

The YAML schema only shows a single `output_type` string:

```yaml
output_type: DiagnosticReport
```

But the output type resolution section says unknown types are resolved through an `importlib` fallback using `output_type_module` + `output_type_class` fields in YAML.

Those fields do not exist in the schema example, which means the core contract for custom output models is still ambiguous.

#### Why this matters

This is not just a documentation nit. It affects:

- what `AgentDefinition` validates
- how users author custom agent definitions
- whether task 1 and task 3 can be implemented consistently
- whether migration of non-built-in agents works at all

If the fallback path is real, it needs to be first-class in the schema. If it is not real, the resolver should not promise it.

#### Recommendation

Pick one explicit contract:

- **Option A:** keep `output_type: str` plus add `output_type_module` to the schema
- **Option B:** replace `output_type` with a structured object
- **Option C:** defer custom output types and keep Phase 2 built-in only

Right now the document says two different things about the same feature.

### 2. Major: the generic loop design does not provide a hook for domain-specific post-dispatch state handling

Phase 2 says `AgentLoop` will be generalized from Phase 1b’s `MonitoringLoop`. But the current monitoring loop only works correctly because it contains domain-specific local state repair after dispatch. The generic design describes configurable dispatch, but not any callback or hook for post-dispatch state mutation/suppression.

#### Why this matters

Without a post-dispatch hook, generic periodic agents will be limited to a very narrow pattern:

- detect issue
- dispatch another agent
- hope repeated polling does not rediscover the same issue forever

Phase 1b already demonstrated that “dispatch only” was not enough; it needed local mutation to prevent repeated redispatch.

If Phase 2 generalizes the loop but drops the hook that made it work, the template becomes less capable than the concrete implementation it replaces.

#### Recommendation

Add an explicit lifecycle hook to the design, for example:

- `post_dispatch_callback`
- `state_reducer`
- `on_successful_dispatch`

This can still be Python-loaded via module/function reference, just like tools and output validators.

### 3. Major: the design includes `skills` in the schema, but does not define how skills are actually activated

The YAML schema includes:

```yaml
skills:
  - openshift-troubleshooting
  - root-cause-analysis
```

But the rest of the design never explains how the generic runtime:

- mounts or locates the skills
- turns a list of names into an active `SkillsCapability`
- handles missing skills
- distinguishes between dev volume mounts and derived-image production packaging

#### Why this matters

This creates a hidden implementation gap. A user reading the schema will assume skills are a supported part of the Phase 2 template contract, but the runtime design currently only specifies:

- tool loading
- output type loading
- validator loading
- lifecycle selection

That means `skills` is presently declarative only, not operational.

#### Recommendation

Either:

- define the Phase 2 skills activation contract explicitly, or
- remove `skills` from the YAML schema and mark it deferred

Leaving it half-specified will create drift between the YAML contract and the actual runtime.

### 4. Major: `dispatch_to` and `dispatch_endpoint` create two sources of truth for downstream agent routing

The periodic-loop schema includes both:

- `dispatch_to: diagnostic-agent`
- `dispatch_endpoint: http://diagnostic-agent:8080`

That duplicates routing authority in the same spec.

#### Why this matters

This creates easy configuration drift:

- the name says one thing
- the URL points somewhere else
- the runtime has to decide which one wins

Phase 1 already had to tighten discovery/endpoint authority. Reintroducing dual routing fields in Phase 2 repeats the same category of ambiguity.

#### Recommendation

Pick one source of truth:

- **Registry-driven:** `dispatch_to` only, resolved by registry/config
- **Direct endpoint-driven:** `dispatch_endpoint` only

If both are kept, the precedence and validation rules need to be explicitly documented.

### 5. Medium: the migration path understates how much existing behavior is bound up in Python modules rather than just config

The migration section says:

1. build generic runtime
2. create `agent.yaml` files from existing Python constants
3. build template image
4. verify existing E2E tests pass
5. deprecate per-agent Containerfiles

That sounds straightforward, but the current agent behavior is not just “constants”:

- tool registration is Python
- output validators are Python
- loop behavior is Python
- post-dispatch state mutation is Python

So the real migration is not “constants to YAML”; it is “constants to YAML plus preserving Python hook behavior through a new loading contract.”

#### Why this matters

This makes the task estimate optimistic and the migration story sound simpler than it is. The hard part is not emitting YAML; it is preserving semantics while moving from hardcoded agent construction to a template loader.

#### Recommendation

Reword the migration path to reflect the real dependency chain:

- YAML captures declarative parts
- Python hook modules still carry behavioral parts
- parity is only reached when both config and hook-loading semantics match existing agents

That makes the plan more honest and easier to implement correctly.

## Open Questions / Assumptions

1. Is Phase 2 supposed to fully support skills, or only declare them for a later implementation?
2. Should generic periodic agents be allowed to mutate local state after successful dispatch, and if so, how is that expressed in YAML?
3. Is agent-to-agent dispatch intended to be resolved by registry name or direct endpoint URL?
4. Are custom output models truly in Phase 2 scope, or should the template stay built-in-only for the first generic runtime iteration?

## Summary

The overall direction is strong: the design correctly identifies the duplication across the current per-agent images and proposes a reasonable template-based runtime. The main weakness is not the idea, but the contract completeness.

The current design is strongest on:

- container/runtime templating
- tool import contract
- validator signature definition

It is weakest on:

- custom output type schema consistency
- skills activation semantics
- generic loop extensibility
- routing authority for dispatch

If those four edges are tightened, the Phase 2 design will be much more implementation-ready.
