# Sandbox Runtime Gaps

This note captures the code-to-doc gaps in the `### Sandbox Runtime` section of `docs/ARCHITECTURE.md`.

Goal: identify which claims are already supported by code, which are only partly true, and which represent interface drift or incomplete implementation.

## Section Reviewed

Current architecture claims:

- A spawned runtime is an HTTP service that executes a step with agent-specific configuration supplied by the workflow engine
- The current implementation passes provider and runtime configuration through environment variables and optional mounted content such as skills
- `LIGHTSPEED_PROVIDER` identifies the LLM provider
- `LIGHTSPEED_MODEL` identifies the model
- `credentials_secret` provides credential material
- `/app/skills/` contains optional domain knowledge packages from a skills OCI image
- The runtime interface should be treated generically, and exact route shapes and adapters are implementation details

## What The Code Clearly Supports

### 1. The workflow engine invokes spawned runtimes over HTTP

The workflow sandbox activity spawns a runtime, waits for readiness, builds a request payload, and calls the runtime over HTTP.

Implication:
- The "spawned runtime is an HTTP service" framing is correct for the current workflow execution path.

Code paths:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/spawner/base.py`

### 2. Provider configuration is passed into the spawned runtime

The workflow sandbox activity sets provider-related environment variables before spawning the runtime.

Implication:
- `LIGHTSPEED_PROVIDER` and `LIGHTSPEED_MODEL` are real parts of the current workflow-side spawn contract.
- Additional deployment-specific provider settings are also propagated when present.

Code paths:
- `src/cloud_agents/workflow/temporal_activities.py`

### 3. Optional skills content is mounted into `/app/skills`

Both Kubernetes and Podman spawners support mounting a skills image into `/app/skills`.

Implication:
- The `/app/skills` reference in the doc is supported by current spawner code.

Code paths:
- `src/cloud_agents/spawner/kubernetes_spawner.py`
- `src/cloud_agents/spawner/podman_spawner.py`

### 4. The workflow payload includes prompt, context, optional instructions, and optional output schema

The workflow sandbox activity sends all of these to the spawned runtime.

Implication:
- The workflow path does carry more than just a plain prompt.
- The runtime contract already includes workflow-derived context and optional structured-output hints.

Code paths:
- `src/cloud_agents/workflow/temporal_activities.py`

## Gaps Between Doc And Code

### Gap 1. There is no single unified runtime HTTP contract in the repo today

Status: major interface drift

Why:
- The workflow sandbox activity currently calls `POST /v1/agent/run`.
- The in-repo generic runtime currently serves `POST /v1/run`.
- The workflow request body uses `query`, `context`, `systemPrompt`, and `outputSchema`.
- The in-repo generic runtime request model expects `prompt` and `context`.

Evidence:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/runtime/server.py`
- `src/cloud_agents/models.py`

Decision to make:
- Is `lightspeed-agentic-sandbox` the canonical runtime contract, with local runtime code needing to align?
- Or should the workflow path be updated to the in-repo runtime contract?

Possible implementation work:
- Pick one canonical route and request schema.
- Update the workflow activity and runtime server to match.
- Add contract tests to prevent future drift.

### Gap 2. "The current implementation passes provider and runtime configuration through environment variables"

Status: partly true, but incomplete if read broadly

Why:
- The workflow sandbox path does pass provider config through env vars.
- But the in-repo generic runtime also depends on mounted configuration files like `agent.yaml` and `registry.yaml`.
- So env vars are not the whole runtime configuration story across the repo.

Evidence:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/runtime/generic_entrypoint.py`

Decision to make:
- Is this section meant to describe only the workflow-spawned sandbox path, or all supported runtime variants?

Possible implementation work:
- No code change needed if the section is explicitly scoped to workflow-spawned sandbox execution.
- Otherwise expand the doc to mention file-based runtime config as well.

### Gap 3. "Credential Secret (via credentials_secret) | K8s Secret or env var with API key"

Status: directionally correct, but wording is too narrow

Why:
- The code treats `credentials_secret` as provider credential material.
- It may be mounted as a Kubernetes Secret.
- It may also map to an env var name in non-Kubernetes environments.
- The value is not necessarily just a single API key.

Evidence:
- `src/cloud_agents/workflow/temporal_models.py`
- `src/cloud_agents/workflow/temporal_activities.py`

Decision to make:
- The doc should probably say "provider credentials" instead of "API key".

Possible implementation work:
- Likely just a doc change unless we want more explicit credential typing in the model.

### Gap 4. The section implies a more unified runtime abstraction than the code currently demonstrates

Status: somewhat overstated

Why:
- The doc says "a spawned runtime is an HTTP service" and then correctly warns that route shapes are implementation details.
- But the repo currently contains both a workflow-to-sandbox path and a generic runtime implementation with different expectations.
- The abstraction is headed in the right direction, but it is not yet fully unified in code.

Evidence:
- `src/cloud_agents/workflow/temporal_activities.py`
- `src/cloud_agents/runtime/server.py`
- `src/cloud_agents/runtime/generic_entrypoint.py`

Decision to make:
- Do we want to preserve multiple runtime variants intentionally, or converge on one?

Possible implementation work:
- If convergence is the goal, add a small compatibility layer or migrate both sides to one contract.

### Gap 5. Optional runtime inputs beyond skills are underspecified

Status: incomplete doc coverage

Why:
- The workflow path can also inject MCP server config and secret-backed MCP headers.
- Additional provider env vars like `LIGHTSPEED_PROVIDER_URL`, `LIGHTSPEED_PROVIDER_PROJECT`, and region/version settings are also forwarded.
- The current section only mentions provider, model, credentials, and skills.

Evidence:
- `src/cloud_agents/workflow/temporal_activities.py`

Decision to make:
- Do we want this section to document just the core inputs, or all meaningful runtime inputs?

Possible implementation work:
- Doc-only change if we want more completeness.

## Highest-Value Implementation Candidates

If we want to close code gaps instead of only rewording docs, these seem like the highest-value items:

1. Unify the runtime HTTP contract
- Choose one route and one request schema.
- Update workflow activity and runtime server accordingly.
- Add contract tests.

2. Clarify the runtime model in docs
- Explicitly distinguish the workflow-spawned sandbox path from the generic runtime path, or document the unification plan.

3. Expand credential/runtime input clarity
- Broaden "API key" wording to "provider credentials".
- Optionally mention MCP server config and additional provider env vars.

## Recommended Doc Changes Even If We Implement Nothing

If code stays as-is, the section should say something closer to:

- A workflow-spawned runtime is an HTTP service invoked by the workflow engine.
- The current workflow sandbox path passes provider configuration through environment variables and optional mounted content such as skills and secrets.
- Skills may be mounted into `/app/skills`.
- The runtime contract is still being unified; exact routes and request fields are implementation details and are not yet fully consistent across runtime variants in this repo.

## Open Questions

1. Which HTTP contract should be canonical: the workflow sandbox path or the in-repo generic runtime path?
2. Is the generic runtime intended to replace the sandbox contract, or coexist with it?
3. Should the section document only the workflow-spawned sandbox path, or the broader runtime architecture?
4. Do we want to document MCP server injection in this section, or keep it focused on the core runtime inputs?
