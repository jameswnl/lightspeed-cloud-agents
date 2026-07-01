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

## T36: Stream agent work-in-progress to callers — DEFERRED

Deferred from this implementation cycle. Needs architecture design discussion.

Full design draft, reviewer feedback (2 rounds), and open architecture questions
are captured in [gaps-implementation-plan.md T36](../gaps-implementation-plan.md#t36-stream-agent-work-in-progress-to-callers-phase-1).

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
