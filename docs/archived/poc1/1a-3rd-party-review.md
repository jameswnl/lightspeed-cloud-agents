# Review: Phase 1a Implementation

## Findings

### 1. Major: agent execution failures are normalized into HTTP 200 responses, so callers can silently treat a broken run as successful

`run_diagnostic()` catches every exception and returns `AgentRunResponse(success=False, ...)` with a normal 200 response. `RemoteAgentClient.run()` only raises on transport or non-200 failures; for a 200, it returns the envelope without checking `success`. Once this is wired into the core pod, a dead or misconfigured agent can look like a valid response unless every caller remembers to inspect `success` manually.

#### Why this matters

This is a behavioral contract issue, not a formatting issue. A model/backend failure is being encoded as a successful HTTP exchange instead of an application failure. That makes the integration path fragile because the caller has to remember two error channels:

- transport-level failure via exceptions
- application-level failure via `success=False`

#### Recommendation

Pick one consistent contract:

- **Option A:** runtime failures return HTTP 5xx, and `RemoteAgentClient` keeps raising on those
- **Option B:** keep the envelope, but make `RemoteAgentClient` raise when `success` is `False`

Right now it is too easy for future callers to accidentally ignore an error.

### 2. Major: the new config contract is described as authoritative, but it does not actually validate endpoints or agent types

Phase 1a moves discovery into config, so bad config should fail fast at startup. Instead, `endpoint` and `type` are both plain `str` fields, which means obviously invalid values pass model validation and only fail later at runtime.

#### Why this matters

The design and commit message frame config-driven discovery as a key decision. That makes validation part of the implementation, not an optional enhancement.

Today these invalid values would pass:

- malformed URLs like `htp://diag`
- typos like `diagostic`
- unsupported types beyond the intended set

#### Recommendation

Tighten the schema so the model enforces what the docs claim:

- use `AnyHttpUrl` for `endpoint`
- use a `Literal[...]` or enum for `type`
- add negative tests for invalid URL and invalid type values

That would make Phase 1a’s “authoritative config” claim true in practice.

### 3. Medium: the deployment and E2E coverage do not validate the advertised cross-pod HTTP communication path

The new infra and behave tests only stand up and probe the diagnostic agent directly. There is no deployed core service, no `RemoteAgentClient` hop, and no containerized test that proves the core pod can actually call the agent over HTTP. So the commit proves "agent pod HTTP server works," not "cross-pod delegation works."

#### Why this matters

The central architectural change in Phase 1a is replacing in-process delegation with pod-to-pod HTTP. That substitution is only unit-tested right now.

The current deployment/test path verifies:

- the diagnostic agent container builds
- `/healthz` responds
- `/v1/run` responds directly

But it does **not** verify:

- the core pod reads config and builds a registry
- the core pod instantiates `RemoteAgentClient`
- a real HTTP call from the core service to the diagnostic agent succeeds

#### Recommendation

Before calling the Phase 1a runtime path complete, add at least one deployed integration test that exercises:

1. config-loaded endpoint
2. `RemoteAgentClient`
3. diagnostic-agent pod

Without that, the “cross-pod communication” claim is still partially inferred rather than demonstrated.

### 4. Medium: request context is accepted by the API contract but discarded by the actual agent runner

The request model explicitly allows metadata like `correlation_id` and `trace_id`, but `run_diagnostic()` only forwards `request.prompt` into the agent. None of the request context survives into execution, so the current implementation cannot propagate trace/correlation metadata even though the contract suggests it can.

#### Why this matters

This is subtle because the server test verifies that context reaches the runner boundary, but there is no test that it survives beyond that point. As written, the API advertises extensibility for observability and chaining, but the implementation drops it immediately.

#### Recommendation

Either:

- thread context through the runner now, even if only for logging/trace placeholders, or
- explicitly document that context is reserved for future use and not yet consumed

If left as-is, the contract overpromises compared to the implementation.

## Open Questions / Assumptions

1. Is `/v1/run` meant to be a stable generic agent protocol for future agent types, or still a diagnostic-agent-focused Phase 1a contract?
2. Is the intended Phase 1a definition of done "working agent pod + client abstraction", or "deployed end-to-end cross-pod call proven in containers"?
3. Should config-driven discovery be validated strictly now, or is looser validation intentionally deferred?

## Summary

The implementation is substantial and mostly well-structured. The new `src/agents/` layout is coherent, the unit coverage for the new code is strong, and adding `output_type` and `schema_version` to the response envelope was the right move for future multi-agent support.

The biggest gap is that the "Phase 1a complete" claim is a little ahead of what the runtime path proves today:

- failure semantics are not yet crisp
- config validation is weaker than the design implies
- deployed cross-pod communication is not fully exercised
- request context is modeled but not used

That does not make the work bad; it means the foundation is good, but a few key contract edges still need tightening before I would call the HTTP handoff fully production-shaped.
