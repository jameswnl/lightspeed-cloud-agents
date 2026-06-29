# Sandbox HTTP Contract

Contract between the Temporal workflow-runner and `lightspeed-agentic-sandbox` pods.

## Endpoint

```
POST /v1/agent/run
```

## Request

```json
{
  "query": "string (required) — the agent prompt",
  "context": {
    "targetNamespaces": ["string"] ,
    "previousAttempts": [
      {"step": "string", "error": "string", "output": {}}
    ],
    "approvedOption": {"id": "string", "action": "string"},
    "executionResult": {}
  },
  "systemPrompt": "string (optional) — inline system instructions",
  "outputSchema": {} 
}
```

### Context Sections

| Section | When populated | Source |
|---------|---------------|--------|
| `targetNamespaces` | Step has `target_namespaces` config | Workflow YAML |
| `previousAttempts` | Prior steps failed | Step results with `status=failed` |
| `approvedOption` | Execution step after approval | Analysis step options + approval `selected_option_id` |
| `executionResult` | Verification step after execution | Execution step output |

## Response

### Success (HTTP 200)

```json
{
  "success": true,
  "output": {
    "summary": "string — human-readable result",
    ...
  }
}
```

### Application Failure (HTTP 200)

```json
{
  "success": false,
  "error": "string — what went wrong",
  "output": {}
}
```

**Not retried** by Temporal. The agent completed but produced a failure result. The workflow marks the step as `failed` and may escalate.

### Infrastructure Error (HTTP 502)

```json
{
  "error": "string — infrastructure error description"
}
```

**Retried** by Temporal via `RetryPolicy`. Infrastructure errors include: LLM provider timeout, connection refused, rate limit exceeded, API key invalid.

## Health Endpoint

```
GET /healthz
```

Returns HTTP 200 when the sandbox is ready to accept requests. The spawner polls this endpoint (configurable via `SpawnConfig.health_path`, default `/healthz`) before calling `/v1/agent/run`.

## Environment Variables

The workflow-runner sets these on spawned sandbox pods:

### From ProviderConfig (workflow definition)

| Env Var | Source | Required |
|---------|--------|----------|
| `LIGHTSPEED_PROVIDER` | `ProviderConfig.name` | Yes |
| `LIGHTSPEED_MODEL` | `ProviderConfig.model` | Yes |

### From Deployment Config (workflow-runner env)

These are forwarded from the workflow-runner's own environment to sandbox pods:

| Env Var | Purpose | Required |
|---------|---------|----------|
| `LIGHTSPEED_MODEL_PROVIDER` | SDK provider mapping | Provider-dependent |
| `LIGHTSPEED_PROVIDER_URL` | LLM endpoint URL | Provider-dependent |
| `LIGHTSPEED_PROVIDER_PROJECT` | GCP project (Vertex AI) | Gemini only |
| `LIGHTSPEED_PROVIDER_REGION` | GCP region (Vertex AI) | Gemini only |
| `LIGHTSPEED_PROVIDER_API_VERSION` | API version override | Optional |

### Credentials

| Env Var | Delivery |
|---------|----------|
| `OPENAI_API_KEY` | K8s: `SecretKeyRef` from `credentials_secret`. Podman: host env propagation |
| `ANTHROPIC_API_KEY` | Same pattern |
| `GOOGLE_API_KEY` | Same pattern |

## Volume Mounts

| Path | Content | Mount mode |
|------|---------|------------|
| `/app/skills` | Skills from OCI image (init container or named volume) | Read-only |

## Labels

Spawned sandbox pods carry these labels for lifecycle management:

| Label | Value |
|-------|-------|
| `cloud-agents/workflow-id` | Workflow execution ID |
| `cloud-agents/step-name` | Step name within the workflow |
| `cloud-agents/attempt` | Retry attempt number |

Used for: label-based orphan cleanup after worker crashes.
