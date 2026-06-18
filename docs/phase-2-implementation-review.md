# Review: Phase 2 Implementation

## Findings

### 1. Major: the generic runtime advertises skills in the YAML contract, but the implementation ignores them completely

Both shipped agent definitions declare `skills`, and the Phase 2 plan said skills were part of the runtime identity. But the generic runtime never actually activates them.

`create_generic_runner()` resolves:

- output type
- tools
- output validator

but does not construct or pass any `SkillsCapability`, and `generic_entrypoint.py` does not do it either.

#### Why this matters

This creates a contract violation:

- the YAML says skills are part of the runtime definition
- the runtime silently ignores them
- migrated agents can start successfully while behaving differently from the old per-agent images

That is the kind of drift that unit tests can miss while users still get the wrong runtime behavior.

#### Recommendation

Either:

- actually wire skills into generic runtime creation, or
- remove/disable the skills contract until it is operational

A declarative field that is silently ignored is worse than a deferred feature that fails explicitly.

### 2. Major: the commit claims generic-image E2E parity, but the checked-in deployment assets still target the old per-agent images

The repo now contains `deploy/agent-runtime/Containerfile`, but the existing Kind manifests and Podman compose file still reference:

- `diagnostic-agent` image
- `monitoring-agent` image

There are no committed deployment changes that mount:

- `/app/agent.yaml`
- `/app/registry.yaml`
- `/app/tools`

into the generic image path.

#### Why this matters

This makes the “9 E2E scenarios pass against generic image” claim unreproducible from the repository as committed.

The new runtime may have been validated manually, but the repo does not yet encode the migration path in deployable/testable assets.

That is a meaningful gap for a migration-phase implementation, because the infrastructure story is part of the feature.

#### Recommendation

If Phase 2 is considered complete, the checked-in deploy path should show the generic image running in place of the per-agent images, or the claim should be softened to “generic runtime validated manually” rather than “existing E2E scenarios pass against generic image.”

### 3. Major: the generic entrypoint still hardcodes cluster-state initialization, so the bootstrap path is not truly generic

`build_app()` always imports and initializes `agents.diagnostic.cluster_state`, regardless of the actual agent definition being loaded.

#### Why this matters

That means the generic bootstrap path still assumes:

- cluster simulation exists
- agent behavior is cluster-oriented
- the runtime has direct knowledge of a specific domain module

This is fine for the two current agent types, but it contradicts the broader claim that one image can serve arbitrary future agent types based purely on mounted config.

A truly generic runtime should not require a domain-specific simulation module in its startup path unless that dependency is expressed in the agent definition itself.

#### Recommendation

Move cluster-scenario initialization out of the generic entrypoint and into an explicit lifecycle hook or tool/bootstrap callback for the agents that actually need it.

That preserves current behavior without hardcoding a cluster-specific assumption into the generic runtime.

## Verification

I ran the new Phase 2 unit suite:

```bash
uv run pytest \
  tests/unit/agents/runtime/test_generic_runner.py \
  tests/unit/agents/runtime/test_generic_entrypoint.py \
  tests/unit/agents/runtime/test_agent_loop.py \
  tests/unit/agents/runtime/test_tool_loader.py \
  tests/unit/agents/runtime/test_output_types.py \
  tests/unit/agents/runtime/test_model_factory.py \
  tests/unit/agents/test_definition.py -q
```

Result:

- **47 passed**

## Summary

The implementation gets a lot right:

- the schema, loader, output type registry, model factory, generic runner, and generic loop all exist
- the targeted unit suite passes
- the code is reasonably well factored

The biggest remaining problems are not broken unit behavior, but contract and migration gaps:

1. skills are declared but not operational
2. generic image migration is not represented in committed deployment/E2E assets
3. the bootstrap path is still cluster-state-specific rather than truly generic

Those are significant enough that I would not call Phase 2 fully complete yet.
