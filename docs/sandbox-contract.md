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
| `cloud-agents/expires-at` | Unix seconds: ceil(spawn time) + max(per-sandbox lifetime, `SANDBOX_MIN_LIFETIME_SECONDS`) + `SANDBOX_EXPIRY_SKEW_SECONDS` (set only when the caller passes a lifetime) |

Used for: label-based orphan cleanup after worker crashes. On startup a runner
lists `spawned-by=workflow-runner` sandboxes once and destroys exactly those
(by their unique gateway sandbox name, never by agent name) whose
`cloud-agents/expires-at` is in the past, whichever runner spawned them.
Expiry is the only criterion: a runner identity can't prove a sandbox's
previous owner is gone. Sandboxes with no `expires-at` and sandboxes with a
malformed one are left alone and logged. Caller-supplied `expires-at` labels
(and the retired `cloud-agents/owner-instance`) are always dropped.

**Periodic reaper.** Startup reconcile alone would leave a crashed replica's
sandboxes until some runner restarts, so every runner (Temporal and local
entrypoints) also re-runs the same reconcile every `SANDBOX_REAP_INTERVAL_SECONDS`
(default 300; `0` disables it; must be a non-negative integer or startup
fails). Runs are single-flight: a tick that fires while the previous run is
still in progress is skipped. Every reconcile run, startup or periodic,
bounds each list page and each delete at 30s
(`SANDBOX_REAP_CALL_TIMEOUT_SECONDS` in `temporal/entrypoint.py`, via
`await_within()`) and lists at most 10 pages (`SANDBOX_REAP_MAX_PAGES`), so a
hung gateway can't stall startup or the reaper. If listing stops early (page
cap, or a page fails after the first), the sandboxes already listed are still
reaped -- selection is per sandbox -- and a failed page is then raised (at
startup, as a failed first page always was). A periodic run's errors are
logged, never raised. `ls_sandbox_reap_failures_total` has the
`SandboxReapFailure` alert (see `docs/operations/runbook.md`). Failed or timed-out calls are counted in
`ls_sandbox_reap_failures_total{operation="list"|"delete"}`, reaped
sandboxes in `ls_sandbox_orphans_cleaned_total`. The reaper is cancelled on
app shutdown. Replicas reap concurrently, so a delete that finds the
sandbox already gone (gRPC `NOT_FOUND`) counts as success.

**Assumptions.** Expiry is only as safe as the deadlines behind it: it relies
on every phase that can use a sandbox after `expires-at` is stamped being
bounded by an enforced wall-clock deadline, plus clocks between replicas
agreeing within `SANDBOX_EXPIRY_SKEW_SECONDS`. Labels alone guarantee
nothing.

**Per-sandbox lifetime** (`cloud_agents/spawner/lifetime.py`), the sum of the
enforced deadlines measured from when `expires-at` is stamped:

- **Spawn: 420s + 5s cancel grace** (`SANDBOX_SPAWN_DEADLINE_SECONDS`). One
  budget in `OpenShellSpawner._do_spawn`, run through `await_within()` (see
  below), that starts at the same instant `expires-at` is stamped and covers
  CreateSandbox (30s SDK RPC timeout), the 300s gateway
  ready wait, the 60s HTTP readiness check and ~30s of post-create
  exec/expose calls (MCP secret writes, skill materialization, server start,
  ExposeService). On timeout the spawn fails, and the sandbox is deleted on a
  best-effort basis if CreateSandbox returned its name. That failure-path
  cleanup (sandbox delete, provider delete) is bounded too, at 30s per step
  (`SANDBOX_SPAWN_CLEANUP_TIMEOUT_SECONDS`), so it can't block the caller. It
  isn't part of the lifetime, because a sandbox whose spawn failed is no
  longer used. Cancelling the
  awaiting coroutine doesn't stop a blocking SDK call already running in a
  worker thread, so a CreateSandbox can still complete remotely after the
  deadline. That sandbox exists with its `expires-at` label and is reaped
  once expired. If its name is unknown, no delete is guessed.
- **Run: `permissions.timeout_seconds`** (default 600) **+ 5s cancel grace.**
  Both engines enforce it as an overall deadline on `POST /v1/agent/run`
  *including the HTTP client's close*, since httpx's own timeout is per read.
  Temporal's `start_to_close_timeout` can only end a step earlier.
- **Progress-stream cleanup: 5s cancel grace.**
- **Transcript fetch: 30s + 5s grace** (including its client's close).
  **Transcript-store save: 30s + 5s grace.**
- **Destroy grace: 120s.** A margin, not a safety requirement: once the
  transcript save finishes the sandbox is no longer used.

So a step's lifetime is 420 + run timeout + 30 + 30 + 5 × 5 + 120 seconds.

**Bounded vs. abandoned work.** The spawn and each post-spawn phase run through
`await_within()`/`cancel_within()` (`spawner/lifetime.py`). The work runs as
a task that's waited on for at most its deadline. Then it's cancelled and
waited on for at most `SANDBOX_CANCEL_GRACE_SECONDS` (5s). Cooperative I/O
(httpx, asyncpg, asyncio) stops within that grace and is fully bounded. A
task still running after the grace is uncooperative: it's abandoned (logged,
never awaited again) and the step fails or moves on without using the
sandbox from that phase again. In particular, a timed-out run never goes on
to fetch the transcript. Python can't preempt arbitrary code, so abandoned
work may keep running in the background. The bound is on how long the step
itself keeps using the sandbox, not a hard guarantee about uncooperative
code.

Each retry attempt spawns a new sandbox (the attempt number is part of the
`ca-` name; the activity runs with `maximum_attempts=1`), so the lifetime is
per attempt. A lifecycle without an enforced latest-use bound gets no
lifetime and no `expires-at`, so reconcile never reaps it. CLI
sessions are one: their timeout monitor can stop or block.

`SANDBOX_MIN_LIFETIME_SECONDS` (default 86400) and `SANDBOX_EXPIRY_SKEW_SECONDS`
(default 600) are operational margins, not enforcement: the floor only delays
how soon an expired orphan is reaped, and the skew covers clock differences
between replicas. Both must be non-negative integers or startup fails.

**Upgrades and rollbacks.** Runners from before expiry-based reconcile destroy
*every* `spawned-by=workflow-runner` sandbox on startup, so a single old
runner starting up can destroy live sandboxes of new runners. Stop and drain
all old runners **before** any new runner admits work. Running both versions
side by side for any length of time isn't safe. A rollback needs the same
order in reverse: stop and drain the new runners before starting old ones.

**Legacy sandboxes.** Sandboxes spawned by older runners carry no `expires-at`
and are never reaped automatically. Once no old-version runner (and no
workflow it started) is still running, clean them up once with the OpenShell
SDK (add the TLS/bearer-token arguments your gateway needs, as in
`cloud_agents/spawner/factory.py`). CLI session sandboxes also lack
`expires-at` by design, so the script skips them. Run it with `DRY_RUN = True`
first:

```python
from openshell import SandboxClient

DRY_RUN = True
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
        if "cloud-agents/expires-at" not in ref.labels
        and ref.labels.get("cloud-agents/session-type") != "cli-handoff"
    ]
    if len(refs) < page:
        break
    offset += page
# Delete after listing so deletions don't shift later pages.
for name in legacy:
    print("would delete" if DRY_RUN else "deleting", name)
    if not DRY_RUN:
        client.delete(name, workspace="default")
```
