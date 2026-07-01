# Phase 1 — Implementation Plan

**Tasks**: T1, T3, T22
**Focus**: High value, enables other work
**T36 deferred**: Agent progress streaming requires more architecture/design discussion before implementation. Remains Phase 1 priority but not in this implementation cycle.
**Reviewed by**: `implementation-plan-review-round-1.md` — 5 findings addressed; `implementation-plan-review-round-2.md` — 2 findings addressed; T36 deferred per user direction

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

**Scope**: Phase 1 delivers runner-side forwarding only. This is a prerequisite for per-step tool scoping, NOT the complete security gap closure. The sandbox must also consume and enforce `allowedTools`/`deniedTools` — that is a separate upstream task. The parent implementation plan (T1) should remain open until end-to-end enforcement is verified.

**What Phase 1 does NOT deliver**: Sandbox-side tool filtering enforcement. A denied tool will appear in the request body but may still be callable by the agent until the sandbox implements enforcement.

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

### Design decisions

**Side channel**: Option A (direct callback from sandbox to runner). The sandbox POSTs progress events to a callback URL. Simplest, no extra infra.

**Statelessness scope**: Phase 1 uses an in-memory `ProgressStore` in the runner process. This means:
- Progress streaming only works with a single runner replica
- Runner restart drops in-flight progress events
- SSE clients must connect to the same replica that spawned the sandbox

This is explicitly a **single-runner stepping stone**. Multi-replica streaming (via Redis or external event store) is deferred to a future phase. The plan must not claim multi-replica support.

**Callback addressing**: The runner's callback base URL is configured via `WORKFLOW_RUNNER_CALLBACK_URL` env var. The value must be routable from **inside the spawned sandbox container**, not from the operator's host shell. Examples:
- K8s: `http://workflow-runner.{namespace}.svc:8080` (Service DNS, reachable from any pod in the cluster)
- Podman: `http://workflow-runner:8080` (container DNS on shared Podman network)
- Dev (Podman): `http://host.containers.internal:8080` (Podman's host gateway, when runner runs on the host)

`localhost` is NOT valid — from inside the sandbox container, `localhost` refers to the sandbox itself, not the runner.

If `WORKFLOW_RUNNER_CALLBACK_URL` is not set, progress streaming is disabled (no `progressUrl` in request body). This makes it opt-in.

**Callback authentication**: Each sandbox spawn generates a per-step callback token (random UUID). The token is:
1. Passed to the sandbox in the request body as `progressToken`
2. Required as `Authorization: Bearer {token}` on progress callback POSTs
3. Validated by the progress ingestion endpoint — reject missing/invalid tokens

**Event identity**: Progress events are keyed by `(workflow_id, step_name, attempt)`, not just `workflow_id`. This handles parallel steps and retries correctly. Cleanup happens per-key when the activity completes (success or failure).

### Code changes

**`src/cloud_agents/workflow/progress_store.py`** (new) — in-memory buffer:
```python
class ProgressStore:
    """In-memory buffer for agent progress events.
    
    Keyed by (workflow_id, step_name, attempt). Single-replica only in Phase 1.
    """
    def append(self, workflow_id: str, step_name: str, attempt: int, event: dict): ...
    def read_since(self, workflow_id: str, step_name: str | None, cursor: int) -> list[dict]: ...
    def cleanup(self, workflow_id: str, step_name: str, attempt: int): ...
    def register_token(self, workflow_id: str, step_name: str, token: str): ...
    def validate_token(self, workflow_id: str, step_name: str, token: str) -> bool: ...
```

**`src/cloud_agents/workflow/temporal_api.py`** — add authenticated progress ingestion:
```python
@router.post("/{workflow_id}/steps/{step_name}/progress")
async def ingest_progress(workflow_id: str, step_name: str, request: Request, event: dict):
    """Receive progress events from sandbox containers."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not progress_store.validate_token(workflow_id, step_name, token):
        raise HTTPException(status_code=403, detail="Invalid progress token")
    progress_store.append(workflow_id, step_name, attempt=event.get("attempt", 1), event=event)
```

**`src/cloud_agents/workflow/temporal_activities.py`** — generate token, build callback URL, pass to sandbox:
```python
callback_base = os.environ.get("WORKFLOW_RUNNER_CALLBACK_URL")
if callback_base:
    import uuid
    progress_token = str(uuid.uuid4())
    progress_store.register_token(workflow_id, step_name, progress_token)
    request_body["progressUrl"] = f"{callback_base}/v1/workflows/{workflow_id}/steps/{step_name}/progress"
    request_body["progressToken"] = progress_token
```

**`src/cloud_agents/workflow/temporal_api.py`** — enrich SSE endpoint:
```python
# existing workflow events + progress events interleaved
progress_events = progress_store.read_since(workflow_id, step_name=None, cursor=progress_cursor)
for event in progress_events:
    yield f"data: {json.dumps(event)}\n\n"
```

### Tests

**Unit tests — ProgressStore**:

```
TestProgressStore:

1. test_append_and_read_by_step
   - Append 3 events for ("wf-1", "diagnose", 1)
   - read_since("wf-1", "diagnose", 0) returns all 3
   - read_since("wf-1", "diagnose", 2) returns only the 3rd

2. test_read_all_steps
   - Append to ("wf-1", "diagnose", 1) and ("wf-1", "fix", 1)
   - read_since("wf-1", step_name=None, 0) returns events from both steps

3. test_read_empty
   - read_since("wf-nonexistent", None, 0) returns []

4. test_cleanup_per_step
   - Append to ("wf-1", "diagnose", 1) and ("wf-1", "fix", 1)
   - cleanup("wf-1", "diagnose", 1)
   - read_since for "diagnose" returns []
   - read_since for "fix" still returns its events

5. test_parallel_steps_isolated
   - Append to ("wf-1", "step-a", 1) and ("wf-1", "step-b", 1) concurrently
   - Each read_since returns only its own events

6. test_retry_attempts_isolated
   - Append to ("wf-1", "diagnose", 1) and ("wf-1", "diagnose", 2)
   - read_since with step_name="diagnose" returns events from both attempts in order
```

**Unit tests — Token auth**:

```
TestProgressTokenAuth:

7. test_register_and_validate_token
   - register_token("wf-1", "diagnose", "tok-abc")
   - validate_token("wf-1", "diagnose", "tok-abc") → True

8. test_invalid_token_rejected
   - register_token("wf-1", "diagnose", "tok-abc")
   - validate_token("wf-1", "diagnose", "wrong-token") → False

9. test_missing_token_rejected
   - No token registered
   - validate_token("wf-1", "diagnose", "any") → False

10. test_token_scoped_to_step
    - register_token("wf-1", "diagnose", "tok-abc")
    - validate_token("wf-1", "fix", "tok-abc") → False (different step)
```

**Unit tests — Progress ingestion endpoint**:

```
TestProgressIngestion:

11. test_valid_token_accepted
    - Register token, POST to /v1/workflows/wf-1/steps/diagnose/progress with Bearer token
    - Assert 200, event stored

12. test_invalid_token_returns_403
    - POST with wrong Bearer token → 403

13. test_missing_auth_header_returns_403
    - POST without Authorization header → 403
```

**Unit tests — SSE enrichment**:

```
TestSSEWithProgress:

14. test_sse_includes_progress_events
    - Store progress events for "wf-1"
    - GET /v1/workflows/wf-1/events
    - Assert SSE stream includes both workflow events AND progress events

15. test_sse_without_progress_backward_compatible
    - No progress events stored
    - Assert SSE stream has only workflow events
```

**Unit tests — Callback URL forwarding**:

```
TestProgressUrlForwarding:

16. test_progress_url_included_when_callback_configured
    - Set WORKFLOW_RUNNER_CALLBACK_URL="http://runner:8080"
    - Call run_sandbox_step
    - Assert request_body includes progressUrl and progressToken

17. test_no_callback_url_no_progress_fields
    - WORKFLOW_RUNNER_CALLBACK_URL not set
    - Call run_sandbox_step
    - Assert request_body does NOT contain progressUrl or progressToken

18. test_progress_token_is_unique_per_step
    - Call run_sandbox_step twice for different steps
    - Assert progressToken values are different
```

**Integration test** (with real Temporal):

```
19. test_progress_events_flow_end_to_end
    - Start workflow with stub spawner that spawns, then POSTs progress events to the callback URL
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
```

All three can run in parallel. Total effort: ~1.5 days.

T36 (streaming) deferred — needs architecture design discussion first.

## Verification Checklist

- [ ] T1: `allowedTools`/`deniedTools` in sandbox POST body when step has permissions
- [ ] T1: No tool fields when step has no permissions
- [ ] T3: `GET /metrics` returns `ls_sandbox_cleanup_failures_total`
- [ ] T3: `GET /metrics` returns `ls_sandbox_orphans_cleaned_total`
- [ ] T3: Failed destroy increments failure counter
- [ ] T22: `model_provider` in ProviderConfig → overrides env var
- [ ] T22: No `model_provider` → falls back to env var
- [ ] All existing tests still pass (307 baseline)
