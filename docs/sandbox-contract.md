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
    "executionResult": {},
    "executionContext": {}
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
| `executionContext` | Workflow/step `context` is set (issue #268; step values win per key over workflow values) | Merged workflow+step `context` |

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
| `spawned-by` | `workflow-runner` (always set) |
| `cloud-agents/agent-name` | Agent name (always set; the gateway assigns the sandbox name) |
| `cloud-agents/owner-instance` | Runner replica's identity: `RUNNER_INSTANCE_ID`, else `HOSTNAME` (set only when one is available) |
| `cloud-agents/expires-at` | Unix seconds: spawn time + `SANDBOX_MAX_LIFETIME_SECONDS` (default 86400) |

Used for: label-based orphan cleanup after worker crashes. On startup a runner
makes one `spawned-by=workflow-runner` listing and destroys sandboxes whose
`cloud-agents/owner-instance` matches its own identity (the Helm chart sets
`RUNNER_INSTANCE_ID` to the pod name), plus any sandbox whose
`cloud-agents/expires-at` is in the past regardless of owner (e.g. left by a
replaced pod). Other replicas' unexpired sandboxes, sandboxes with no
`expires-at` label (spawned before it existed) and sandboxes with a malformed
`expires-at` are left alone and logged. With no identity, only expired
sandboxes are destroyed.

An explicit `RUNNER_INSTANCE_ID` must be a valid label value or startup fails;
an invalid `HOSTNAME` fallback only disables own-instance cleanup with a
warning. `SANDBOX_MAX_LIFETIME_SECONDS` below 3900 (a CLI session's default
3600s plus the 300s sandbox ready wait) fails startup. A workflow step whose
`timeout_seconds` exceeds the lifetime isn't caught (the schema has no upper
bound) and its sandbox may be reaped by another replica's restart, so keep the
lifetime above your longest step timeout.
