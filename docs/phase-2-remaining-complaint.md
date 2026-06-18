# Remaining Phase 2 Complaint

## Issue

The generic runtime still allows **silent skills degradation** when skills are configured but all configured skill directories are missing.

## Why this is a problem

The runtime is intended to be declarative: if `agent.yaml` says the agent has skills, the running agent should either:

1. start with those skills available, or
2. fail startup clearly

Right now it can do neither. It logs warnings and continues with skills disabled.

## Current behavior

In `src/agents/runtime/generic_runner.py`, `_load_skills()` does this:

- raises if `pydantic-ai-skills` is missing
- warns if configured skill directories do not exist
- returns an empty list if **all** configured directories are invalid

That means a misconfigured agent can still start successfully but behave differently than its declarative config promises.

## Requested fix

Make skills startup fully strict:

- if `spec.skills` is configured and **no valid skill directories exist**, raise a startup error
- do not silently continue with `[]`

## Expected outcome

Once that is fixed and rechecked, I would be comfortable marking the Phase 2 work as approved.
