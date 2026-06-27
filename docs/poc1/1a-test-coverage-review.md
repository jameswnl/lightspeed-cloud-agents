# Review: Phase 1a Test Coverage

## Findings

### 1. Major: the current test suite does not prove the key Phase 1a claim of deployed cross-pod HTTP communication

The E2E layer only talks directly to the diagnostic agent over localhost, and the Kind setup only deploys the diagnostic agent plus Ollama. There is no deployed test path exercising config loading, registry lookup, `RemoteAgentClient`, and then a live HTTP hop into the agent pod.

#### Why this matters

The central architectural change in Phase 1a is replacing in-process delegation with pod-to-pod HTTP. Right now that substitution is:

- **unit-tested** in isolation
- **not deployment-tested** end-to-end

So the suite proves "the agent pod HTTP server works" but not "the core pod can delegate to the agent pod over the new runtime path."

#### Recommendation

Add one deployed integration test that exercises:

1. endpoint loaded from config
2. registry lookup
3. `RemoteAgentClient`
4. live diagnostic-agent pod

Until then, the most important Phase 1a claim is still partially inferred.

### 2. Major: the tests miss the most important failure-contract boundary between the runner and the client

`run_diagnostic()` returns `success=False` inside a 200 response envelope when the agent execution fails. `RemoteAgentClient.run()` raises on transport or non-200 responses, but there is no test covering the case where the remote agent returns an HTTP 200 with a structured error payload.

#### Why this matters

This is exactly the kind of contract bug that slips through strong unit suites:

- server returns a valid envelope
- client accepts it
- caller forgets to inspect `success`

Without a test for that boundary, the suite does not protect the most subtle integration risk in the new HTTP contract.

#### Recommendation

Add a client test for:

- 200 response
- valid `AgentRunResponse`
- `success=False`
- expected caller behavior clearly asserted

That behavior should be decided explicitly, not left as an implementation accident.

### 3. Medium: the E2E assertions are too shallow for the behaviors they claim to validate

The "diagnoses and reports" scenario only checks that certain fields are present, not that the agent actually diagnosed anything meaningful, performed remediation when appropriate, or returned semantically valid output.

#### Why this matters

A test that checks only field presence can pass even if the agent returns:

- an empty-but-well-shaped response
- a low-value summary
- no actual actions
- no meaningful issue detection

That weakens the confidence value of the slow-path tests, which are supposed to validate real behavior rather than just API shape.

#### Recommendation

Strengthen the E2E scenarios so they assert one of these concrete behaviors:

- known degraded state is injected and `actions_taken` is non-empty
- a host transitions from unhealthy to healthy
- healthy baseline returns explicitly empty issues/actions
- expected issue text or state change is present

Presence checks are useful, but they should not be the main proof for agent behavior.

### 4. Medium: coverage is high overall, but the under-tested areas are the bootstrap and runtime edges that matter most

The coverage numbers are good, but the weaker spots are exactly the files that represent runtime realism:

- `src/agents/diagnostic/_model.py`
- `src/agents/diagnostic/entrypoint.py`

Config coverage also focuses on missing required fields, not invalid-but-present values like malformed endpoints or unsupported types.

#### Why this matters

This means the suite strongly protects internal logic but leaves gaps around:

- environment-driven model configuration
- actual startup path
- runtime assembly of the app
- strict validation of the new config-driven discovery contract

Those are the places where real deployments tend to fail.

#### Recommendation

Add focused tests for:

- `_model.py` environment behavior and model caching
- `entrypoint.py` startup behavior
- invalid endpoint values
- invalid agent type values

That would make the suite much better at catching real deployment regressions rather than just logical regressions.

## Coverage Snapshot

I ran:

```bash
uv run pytest tests/unit/agents tests/unit/models/config/test_agent_endpoint_config.py --cov=src/agents --cov=src/models/config --cov-report=term-missing
```

### Result

- **86 tests passed**
- **93% total coverage** across `src/agents`

### Notable weak spots

- `src/agents/diagnostic/_model.py`: **46%**
- `src/agents/diagnostic/entrypoint.py`: **0%**

## Summary

The unit coverage is strong, but the coverage quality is uneven:

- very good on pure logic and happy-path tool/client behavior
- weaker on failure contracts
- weaker on startup/bootstrap realism
- weakest on the deployed integration boundary the feature is actually introducing

This is a solid test base. It just is not yet a great proof suite for the main architectural claim of Phase 1a.

## Suggested Next Tests

If I were tightening this suite next, I would add these in order:

1. **RemoteAgentClient error-envelope test**: HTTP 200 with `success=False`
2. **Strict config validation tests**: malformed `endpoint`, unsupported `type`
3. **`_model.py` tests**: env var handling, caching, provider construction
4. **`entrypoint.py` test**: startup resets state and builds app correctly
5. **Real integration test**: config -> registry -> client -> live agent pod
6. **Stronger E2E semantic assertions**: remediation happened, not just field presence
