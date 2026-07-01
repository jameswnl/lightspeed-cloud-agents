# Phase 1 — Implementation Plan

**Tasks**: T1, T3, T22, T36
**Focus**: High value, enables other work

---

## T1: Forward PermissionScope to sandbox contract

### What changes

`temporal_activities.py` `_run_sandbox_step_inner()` currently extracts `service_account` and `timeout_seconds` from `step.permissions` but ignores `allowed_tools` and `denied_tools`. Add them to the `request_body` sent to the sandbox.

### Code changes

**`src/cloud_agents/workflow/temporal_activities.py`** — after building `request_body`, add:
```python
if permissions.get("allowed_tools"):
    request_body["allowedTools"] = permissions["allowed_tools"]
if permissions.get("denied_tools"):
    request_body["deniedTools"] = permissions["denied_tools"]
```

### Tests

**Unit tests** (in `tests/unit/workflow/temporal/test_activities.py`):

```
TestPermissionScopeForwarding:

1. test_allowed_tools_forwarded_to_sandbox
   - Step has permissions.allowed_tools: ["list_hosts", "check_host"]
   - Call run_sandbox_step with spawner mock
   - Assert request_body sent to sandbox POST includes allowedTools: ["list_hosts", "check_host"]

2. test_denied_tools_forwarded_to_sandbox
   - Step has permissions.denied_tools: ["run_remediation"]
   - Assert request_body includes deniedTools: ["run_remediation"]

3. test_both_allowed_and_denied_forwarded
   - Step has both allowed_tools and denied_tools
   - Assert both fields present in request_body

4. test_no_permissions_no_tool_fields
   - Step has no permissions field
   - Assert request_body does NOT contain allowedTools or deniedTools keys

5. test_permissions_without_tools_no_tool_fields
   - Step has permissions: {service_account: "sa"} but no tool fields
   - Assert request_body does NOT contain allowedTools or deniedTools
```

**What this does NOT test**: Whether the sandbox actually enforces the tool filtering. That's a sandbox-side concern (upstream). This tests that the workflow engine forwards the configuration correctly.

### Effort: Half day

---

## T3: Cleanup failure metrics

### What changes

Add two Prometheus counters so container leaks are visible in dashboards.

### Code changes

**`src/cloud_agents/workflow/temporal_metrics.py`** — add:
```python
ls_sandbox_cleanup_failures_total = Counter(
    "ls_sandbox_cleanup_failures_total",
    "Number of sandbox containers that failed to be destroyed",
    ["step_name"],
)

ls_sandbox_orphans_cleaned_total = Counter(
    "ls_sandbox_orphans_cleaned_total",
    "Number of orphaned sandbox containers cleaned up on startup",
)
```

**`src/cloud_agents/workflow/temporal_activities.py`** — in the `except` block after failed `spawner.destroy()`:
```python
from cloud_agents.workflow.temporal_metrics import ls_sandbox_cleanup_failures_total
ls_sandbox_cleanup_failures_total.labels(step_name=step_name).inc()
```

**`src/cloud_agents/workflow/temporal_entrypoint.py`** — in `reconcile_orphaned_sandboxes()`, after destroying orphans:
```python
from cloud_agents.workflow.temporal_metrics import ls_sandbox_orphans_cleaned_total
ls_sandbox_orphans_cleaned_total.inc(len(orphans))
```

### Tests

**Unit tests** (in `tests/unit/workflow/temporal/test_metrics.py` or new file):

```
TestCleanupMetrics:

1. test_cleanup_failure_increments_counter
   - Mock spawner.destroy() to raise Exception
   - Call run_sandbox_step (with spawner that spawns successfully but fails to destroy)
   - Assert ls_sandbox_cleanup_failures_total counter incremented by 1
   - Assert step_name label is correct

2. test_successful_cleanup_does_not_increment
   - Mock spawner with normal spawn/destroy
   - Call run_sandbox_step
   - Assert ls_sandbox_cleanup_failures_total NOT incremented

3. test_orphan_cleanup_increments_counter
   - Mock spawner.list_active returning ["orphan-1", "orphan-2"]
   - Call reconcile_orphaned_sandboxes
   - Assert ls_sandbox_orphans_cleaned_total incremented by 2

4. test_no_orphans_no_increment
   - Mock spawner.list_active returning []
   - Call reconcile_orphaned_sandboxes
   - Assert ls_sandbox_orphans_cleaned_total NOT incremented
```

**Verification**: `GET /metrics` endpoint returns `ls_sandbox_cleanup_failures_total` and `ls_sandbox_orphans_cleaned_total` in Prometheus format.

### Effort: Half day

---

## T22: Per-workflow model provider derivation

### What changes

Add `model_provider` field to `ProviderConfig`. The activity sets `LIGHTSPEED_MODEL_PROVIDER` from this field, falling back to the worker's env var when not specified.

### Code changes

**`src/cloud_agents/workflow/temporal_models.py`** — add field to `ProviderConfig`:
```python
class ProviderConfig(BaseModel):
    name: Literal["claude", "openai", "gemini"]
    model: str
    credentials_secret: str
    model_provider: str | None = None  # NEW — e.g. "anthropic" for Claude on Vertex
```

**`src/cloud_agents/workflow/temporal_activities.py`** — in env var construction, replace the static forwarding:
```python
# Before (static from worker env):
for deploy_var in ("LIGHTSPEED_MODEL_PROVIDER", ...):
    if val := os.environ.get(deploy_var):
        env_vars[deploy_var] = val

# After (per-workflow override):
if model_provider := provider.get("model_provider"):
    env_vars["LIGHTSPEED_MODEL_PROVIDER"] = model_provider
elif val := os.environ.get("LIGHTSPEED_MODEL_PROVIDER"):
    env_vars["LIGHTSPEED_MODEL_PROVIDER"] = val
# remaining deploy vars stay as-is
for deploy_var in ("LIGHTSPEED_PROVIDER_URL", ...):  # without MODEL_PROVIDER
    if val := os.environ.get(deploy_var):
        env_vars[deploy_var] = val
```

### Tests

**Unit tests** (in `tests/unit/workflow/temporal/test_activities.py`):

```
TestModelProviderDerivation:

1. test_model_provider_from_provider_config
   - Provider has model_provider: "anthropic"
   - Call run_sandbox_step
   - Assert env_vars passed to spawner include LIGHTSPEED_MODEL_PROVIDER="anthropic"

2. test_model_provider_fallback_to_env
   - Provider has NO model_provider field
   - Set os.environ LIGHTSPEED_MODEL_PROVIDER="openai"
   - Call run_sandbox_step
   - Assert env_vars include LIGHTSPEED_MODEL_PROVIDER="openai"

3. test_model_provider_overrides_env
   - Provider has model_provider: "anthropic"
   - Set os.environ LIGHTSPEED_MODEL_PROVIDER="openai"
   - Call run_sandbox_step
   - Assert env_vars include LIGHTSPEED_MODEL_PROVIDER="anthropic" (not "openai")

4. test_no_model_provider_anywhere
   - Provider has NO model_provider
   - os.environ has NO LIGHTSPEED_MODEL_PROVIDER
   - Call run_sandbox_step
   - Assert LIGHTSPEED_MODEL_PROVIDER NOT in env_vars
```

**Model tests** (in `tests/unit/workflow/temporal/test_models.py`):

```
5. test_provider_config_accepts_model_provider
   - ProviderConfig(name="openai", model="gpt-4", credentials_secret="k", model_provider="anthropic")
   - Assert model_provider == "anthropic"

6. test_provider_config_model_provider_defaults_none
   - ProviderConfig(name="openai", model="gpt-4", credentials_secret="k")
   - Assert model_provider is None
```

### Effort: Half day

---

## T36: Stream agent work-in-progress to callers

### What changes

Add a side-channel streaming path so callers can see agent LLM tokens, tool calls, and intermediate results during step execution — not just workflow-level events.

### Design decision needed first

**Which side channel?** Options:

| Option | Pros | Cons |
|--------|------|------|
| **A: Direct SSE from sandbox to runner** | Simple, no infra. Sandbox opens an SSE connection to runner's progress endpoint. | Requires sandbox → runner network path (exists in both K8s and Podman). Sandbox needs to know runner URL. |
| **B: Redis pubsub** | Decoupled. Runner subscribes to `workflow:{id}:progress`. | Requires Redis deployment. |
| **C: Activity-side polling** | Activity polls sandbox's progress endpoint and forwards to a shared store. | Adds latency, defeats purpose of streaming. |

**Recommendation**: Option A (direct SSE). The sandbox already has network access to the runner (same network/namespace). The activity passes a `progressUrl` in the request body. The sandbox POSTs progress events there. The runner's SSE endpoint reads from an in-memory buffer keyed by workflow_id.

### Code changes (Option A)

**`src/cloud_agents/workflow/temporal_api.py`** — add progress ingestion endpoint:
```python
@router.post("/{workflow_id}/progress")
async def ingest_progress(workflow_id: str, event: dict):
    """Receive progress events from sandbox containers."""
    progress_store.append(workflow_id, event)
```

**`src/cloud_agents/workflow/progress_store.py`** (new) — in-memory buffer:
```python
class ProgressStore:
    """In-memory buffer for agent progress events, keyed by workflow_id."""
    def append(self, workflow_id: str, event: dict): ...
    def read_since(self, workflow_id: str, cursor: int) -> list[dict]: ...
    def cleanup(self, workflow_id: str): ...
```

**`src/cloud_agents/workflow/temporal_activities.py`** — pass progress URL to sandbox:
```python
request_body["progressUrl"] = f"http://{runner_host}:{runner_port}/v1/workflows/{workflow_id}/progress"
```

**`src/cloud_agents/workflow/temporal_api.py`** — enrich SSE endpoint to include progress events:
```python
# existing workflow events + progress events interleaved
progress_events = progress_store.read_since(workflow_id, progress_cursor)
for event in progress_events:
    yield f"data: {json.dumps(event)}\n\n"
```

### Tests

**Unit tests**:

```
TestProgressStore:

1. test_append_and_read
   - Append 3 events for workflow "wf-1"
   - read_since("wf-1", 0) returns all 3
   - read_since("wf-1", 2) returns only the 3rd

2. test_read_empty
   - read_since("wf-nonexistent", 0) returns []

3. test_cleanup
   - Append events, cleanup("wf-1")
   - read_since returns []

4. test_multiple_workflows_isolated
   - Append to "wf-1" and "wf-2"
   - Each read_since returns only its own events
```

```
TestProgressIngestion:

5. test_progress_endpoint_accepts_events
   - POST /v1/workflows/wf-1/progress with {"type": "tool_call", "name": "list_hosts"}
   - Assert 200
   - Assert event stored in progress_store

6. test_progress_endpoint_rejects_unknown_workflow
   - (Decision: should it? Or accept any workflow_id since sandbox is trusted?)
```

```
TestSSEWithProgress:

7. test_sse_includes_progress_events
   - Store progress events for "wf-1"
   - GET /v1/workflows/wf-1/events
   - Assert SSE stream includes both workflow events AND progress events

8. test_sse_without_progress_only_workflow_events
   - No progress events stored
   - GET /v1/workflows/wf-1/events
   - Assert SSE stream has only workflow events (backward compatible)
```

```
TestProgressUrlForwarding:

9. test_progress_url_included_in_sandbox_request
   - Call run_sandbox_step
   - Assert request_body sent to sandbox includes progressUrl field

10. test_progress_url_not_included_when_no_runner_host
    - (Decision: is progressUrl always set, or opt-in?)
```

**Integration tests** (with real Temporal):

```
11. test_progress_events_flow_through_sse
    - Start workflow with stub spawner that posts progress events to runner
    - Subscribe to SSE endpoint
    - Assert progress events appear in SSE stream alongside step events
```

### Sandbox-side work (upstream or fork)

The sandbox needs to POST progress events to `progressUrl` during execution. This is a separate task in the sandbox repo:
- Read `progressUrl` from request body
- During LLM streaming: POST `{"type": "llm_token", "text": "..."}` to progressUrl
- During tool calls: POST `{"type": "tool_call", "name": "list_hosts", "input": "..."}` to progressUrl
- On tool result: POST `{"type": "tool_result", "name": "list_hosts", "output": "..."}` to progressUrl

### Effort: 1-2 weeks (runner side + sandbox side)

---

## Execution Order

```
T3 (metrics)  ─── independent, do first (half day)
T22 (provider) ── independent (half day)
T1 (permissions) ─ independent (half day)
T36 (streaming) ── largest task, start after T1/T3/T22 (1-2 weeks)
```

T1, T3, T22 can run in parallel. T36 is the big one and should start once the smaller tasks are verified.

## Verification Checklist

- [ ] T1: `allowedTools`/`deniedTools` in sandbox POST body when step has permissions
- [ ] T1: No tool fields when step has no permissions
- [ ] T3: `GET /metrics` returns `ls_sandbox_cleanup_failures_total`
- [ ] T3: `GET /metrics` returns `ls_sandbox_orphans_cleaned_total`
- [ ] T3: Failed destroy increments failure counter
- [ ] T22: `model_provider` in ProviderConfig → overrides env var
- [ ] T22: No `model_provider` → falls back to env var
- [ ] T36: Progress events flow from sandbox → runner → SSE
- [ ] T36: SSE backward compatible when no progress events exist
- [ ] All existing tests still pass (307 baseline)
