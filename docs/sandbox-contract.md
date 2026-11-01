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
| `cloud-agents/expires-at` | Unix seconds: spawn time + max(per-sandbox lifetime, `SANDBOX_MIN_LIFETIME_SECONDS`) + `SANDBOX_EXPIRY_SKEW_SECONDS` (set only when the caller passes a lifetime) |

Used for: label-based orphan cleanup after worker crashes. On startup a runner
makes one `spawned-by=workflow-runner` listing and destroys sandboxes whose
`cloud-agents/owner-instance` matches its own identity (the Helm chart sets
`RUNNER_INSTANCE_ID` to the pod name), plus any sandbox whose
`cloud-agents/expires-at` is in the past regardless of owner (e.g. left by a
replaced pod). Other replicas' unexpired sandboxes, sandboxes with no
`expires-at` label and sandboxes with a malformed `expires-at` are left alone
and logged. With no identity, only expired sandboxes are destroyed.
Caller-supplied `owner-instance` / `expires-at` labels are always dropped.

**Per-sandbox lifetime** (`cloud_agents/spawner/lifetime.py`). `expires-at`
must never be earlier than the sandbox's latest legitimate end, so each spawn
call site passes its own lifetime:

- Workflow steps (Temporal activity and local engine): max(step
  `timeout_seconds`, `permissions.timeout_seconds`; each defaulting to 600)
  + 300s gateway ready wait + 60s HTTP ready wait + 180s activity heartbeat
  timeout (a timed-out activity only learns of cancellation via a heartbeat)
  + 30s transcript fetch (its own wall-clock bound) + 120s destroy grace. The
  local engine enforces the HTTP timeout as an overall deadline on the run
  call, since httpx's own timeout is per read. Each retry attempt spawns a new sandbox (the attempt
  number is part of the `ca-` name; the activity itself runs with
  `maximum_attempts=1`), so the lifetime is per attempt.
- CLI sessions: `max_session_seconds` + the timeout monitor's check interval
  + 300s + 60s + 120s, but only while the launcher's timeout monitor is
  running; otherwise no lifetime (and no `expires-at`) is set.
- A spawn with no lifetime gets no `expires-at`, so only its owner can reap it.

`SANDBOX_MIN_LIFETIME_SECONDS` (default 86400) is a floor, not a cap: it only
delays how soon another replica may reap an orphan. `SANDBOX_EXPIRY_SKEW_SECONDS`
(default 600) covers clock skew between replicas. Both must be non-negative
integers or startup fails.

**Identity.** An explicit `RUNNER_INSTANCE_ID` must be a valid label value or
startup fails. The `HOSTNAME` fallback logs a warning: runners sharing a host
network (same hostname) must each set a distinct `RUNNER_INSTANCE_ID`. An
invalid `HOSTNAME` only disables own-instance cleanup. Bare-process dev runs
with neither variable set skip own-instance cleanup (expired sandboxes are
still reaped); the Podman compose files set `RUNNER_INSTANCE_ID`.

**Upgrades and rollbacks.** Runners from before owner scoping destroy *every*
`spawned-by=workflow-runner` sandbox on startup, including new runners' live
ones. Don't run old and new runner versions side by side for long: finish the
rollout promptly, or use a `Recreate` deployment strategy. A rollback carries
the same risk in reverse.

**Legacy sandboxes.** Sandboxes spawned by older runners carry neither
`owner-instance` nor `expires-at` and are never reaped automatically. Once no
old-version runner (and no workflow it started) is still running, clean them
up once with the OpenShell SDK (add the TLS/bearer-token arguments your
gateway needs, as in `cloud_agents/spawner/factory.py`):

```python
from openshell import SandboxClient

client = SandboxClient(endpoint="<gateway host:port>")
legacy, offset, page = [], 0, 100
while True:
    refs = client.list(
        workspace="default",
        label_selector="spawned-by=workflow-runner",
        limit=page,
        offset=offset,
    )
    legacy += [
        ref.name
        for ref in refs
        if "cloud-agents/owner-instance" not in ref.labels
        and "cloud-agents/expires-at" not in ref.labels
    ]
    if len(refs) < page:
        break
    offset += page
# Delete after listing so deletions don't shift later pages.
for name in legacy:
    print("deleting", name)
    client.delete(name, workspace="default")
```
