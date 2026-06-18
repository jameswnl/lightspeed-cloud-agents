# Review: `phase-2-template-design.md` (Updated)

## Findings

### 1. Major: `skills.names` is part of the YAML contract, but the activation design still does not show how name filtering actually works

The updated schema now makes skills more concrete:

```yaml
skills:
  directories:
    - /app/skills
  names:
    - openshift-troubleshooting
    - root-cause-analysis
```

And the prose says:

1. read `directories`
2. if `names` is specified, filter to only those skills
3. create `SkillsCapability(...)`

But the implementation sketch still only shows:

```python
return [SkillsCapability(directories=spec.skills.directories)]
```

There is no visible mechanism for applying the `names` filter.

#### Why this matters

This is still a contract gap:

- the YAML implies precise skill selection
- the design sketch only proves directory-wide activation
- tasking and validation cannot be written cleanly until the filtering mechanism is explicit

If `pydantic-ai-skills` supports filtering, the design should show it. If it does not, the YAML contract should not promise it.

#### Recommendation

Make one of these explicit:

- how `names` is passed into `SkillsCapability`
- how the runtime filters discovered skills before activation
- or that `names` is deferred and Phase 2 supports directories only

### 2. Major: `dispatch_to` now avoids dual routing authority, but the plan still does not define how the generic runtime gets the `AgentRegistry`

The updated design improves the schema by removing direct endpoint URLs from the YAML and standardizing on:

```yaml
dispatch_to: diagnostic-agent
```

resolved via `AgentRegistry`.

That fixes one problem, but it leaves another one open: the generic runtime design never explains how a generic agent container acquires the registry data it needs in order to resolve `dispatch_to`.

#### Why this matters

The generic entrypoint is described as reading:

- `/app/agent.yaml`
- `/app/tools/`
- `/app/skills/`

But nothing in the design explains where the mapping

```text
diagnostic-agent -> http://diagnostic-agent:8080
```

comes from inside the container.

Without that, the loop lifecycle is incomplete:

- the YAML names a target agent
- the runtime has no defined source for target resolution

#### Recommendation

Add an explicit registry contract, for example:

- mounted registry YAML
- environment variable source
- config section loaded alongside `agent.yaml`
- runtime injection from the core service

Right now the design says “resolved via AgentRegistry” without defining how the generic runtime obtains that registry.

### 3. Major: the skills contract now exists, but the failure mode is still too permissive for a declarative runtime

The updated skills section says:

```python
try:
    from pydantic_ai_skills import SkillsCapability
    return [SkillsCapability(directories=spec.skills.directories)]
except ImportError:
    logger.warning("pydantic-ai-skills not installed, skills disabled")
    return []
```

That means an agent definition can explicitly request skills, and the runtime may silently continue with those skills disabled.

#### Why this matters

For a generic runtime template, that is dangerous behavior:

- the config says one thing
- the running agent does another
- the process stays healthy
- behavior drift is discovered only indirectly

This is especially problematic for migration verification, because semantic parity can quietly fail while startup still succeeds.

#### Recommendation

Make this strict when skills are configured:

- if `spec.skills` is present and skills support is unavailable, fail startup
- if requested skills are missing, fail startup

Silent fallback is reasonable only when no skills were requested at all.

### 4. Medium: task ordering and migration steps still understate how foundational `model_factory.py` is

The updated migration section is much better about acknowledging Python hook modules, but the task order still places:

- container/template work in Task 7
- shared model config (`model_factory.py`) in Task 8

That ordering is awkward because model creation is not an optional cleanup; it is part of the generic runtime foundation.

#### Why this matters

If generic runner and generic entrypoint are meant to replace per-agent `_model.py` logic, then shared model creation is a prerequisite for high-confidence runtime assembly, not a trailing refactor.

This does not make the design wrong, but it does make the task graph slightly misleading.

#### Recommendation

Move `model_factory.py` earlier in the implementation order, likely before or alongside the generic runner/entrypoint work.

That would make the plan better match the actual dependency chain.

## What Improved

The updated plan is materially better than the first version. The key issues from the earlier review that are now clearly resolved:

- `output_type_module` is now part of the YAML contract
- skills activation is no longer purely implied
- the generic loop now has an explicit `on_dispatch_success` hook
- direct endpoint duplication was removed in favor of `dispatch_to`
- the migration section now correctly acknowledges that Python hook modules preserve behavioral semantics

Those are real improvements and make the design much more implementation-ready.

## Summary

The Phase 2 design is now in much better shape. I no longer see the earlier blockers around output-type ambiguity, generic loop extensibility, or dual routing authority.

The remaining issues are narrower and mostly about making the declarative contract fully honest and executable:

1. define how skill-name filtering actually works
2. define how the generic runtime obtains `AgentRegistry`
3. tighten skills-related startup failures so config cannot silently degrade
4. move shared model creation earlier in the task order

If those are tightened, I would consider the Phase 2 plan close to implementation-ready.
