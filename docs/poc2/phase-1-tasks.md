# PoC2 Phase 1: Temporal + Sandbox Integration

## Context

This phase combines the sandbox adaptations and Temporal workflow engine integration from `temporal-sandbox-architecture.md`. The goal is a working end-to-end pipeline: FastAPI → Temporal workflow → sandbox pod → LLM → structured result — on both Kind and Podman.

The lightspeed-agentic-sandbox is the agent runtime. Temporal is the workflow orchestrator. The existing spawner abstraction (K8s/Podman) creates sandbox pods per step.

## Contracts (from review round 1)

### Approval Timeout Contract

Approval steps declare timeout via `timeout_seconds` (default from config `approval.default_timeout_seconds`, default 86400 = 24h). Enforced by `workflow.wait_condition(..., timeout=timedelta(seconds=step.timeout_seconds))`. On timeout:
- Step status: `"denied"` with `output.reason = "timeout"`
- Event: `step.denied` emitted
- Workflow continues to next step (condition evaluation decides whether to proceed)

### Approved Option Selection Contract

Analysis steps produce options with a stable `id` field. The approval step emits the selected option `id` (not just approve/deny). Context building uses the approval output's `selected_option_id` to look up the matching option from the analysis step, not hardcoded `options[0]`:

```python
# Approval signal includes selected option
await handle.signal(AgentWorkflow.approve, step_name, "approved", selected_option_id="opt-2")

# Context builder resolves approved option by id
approval_output = workflow_steps[approval_key].output
selected_id = approval_output.get("selected_option_id")
analysis_options = workflow_steps[analysis_key].output.get("options", [])
approved_option = next((o for o in analysis_options if o.get("id") == selected_id), analysis_options[0])
```

### Crash-Boundary Cleanup Contract

Three cleanup mechanisms, in order of priority:
1. **Normal path**: `finally: spawner.destroy(pod_name)` in the activity
2. **Worker crash**: spawned pods carry labels `cloud-agents/workflow-id`, `cloud-agents/step-name`, `cloud-agents/attempt`. A label-based reconciler (background task or manual `kubectl delete pods -l cloud-agents/workflow-id=X`) cleans orphans
3. **Activity timeout**: Temporal cancels the activity after `start_to_close_timeout`. The `finally` block runs on cancellation. If the worker is dead, mechanism 2 applies

Tests: `test_crash_after_spawn_cleanup` (verify label-based cleanup finds orphans), `test_cancel_while_running` (verify activity cancellation triggers destroy)

### API Version Contract

Phase 1 uses `/v1/workflows/*` — same as the architecture doc. No v2 prefix. The `WORKFLOW_ENGINE=temporal` env var selects the Temporal backend; the API surface is the same regardless of engine.

### Sandbox Env Var & Credential Contract

Single authoritative contract (from `temporal-sandbox-architecture.md` env var table):

**Provider selection**: `LIGHTSPEED_PROVIDER` (not `LIGHTSPEED_AGENT_PROVIDER`)
**Model selection**: `LIGHTSPEED_MODEL`
**Credentials**:
- **K8s**: `SecretKeyRef` → env var or file mount at `/var/run/secrets/llm-credentials/`
- **Podman**: host env propagation. The Podman spawner reads credentials from the host environment and passes them as container env vars. This is an accepted compromise — Podman deployments are single-host with a single trust domain. The credentials are not in pod specs (no `kubectl describe` exposure) because there is no kubectl.

Provider-specific credential env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) are set by the spawner from the `credentials_secret` field in the workflow YAML.

### Note on companion architecture doc

This phase-1 plan supersedes stale examples in `temporal-sandbox-architecture.md` where they conflict — specifically the approval signal signature (now includes `selected_option_id`) and the provider env var name (`LIGHTSPEED_PROVIDER`, not `LIGHTSPEED_AGENT_PROVIDER`). The architecture doc will be refreshed after Phase 1 implementation.

## Sandbox Adaptations (upstream PRs)

### Task 1: Add `executionResult` handling to sandbox context formatting

**Problem**: `_format_context_prefix()` handles `targetNamespaces`, `previousAttempts`, `approvedOption` but NOT `executionResult`. Verification steps can't see what was executed.

**Change**: ~15 lines in sandbox's `query.py`. Submit as PR to `openshift/lightspeed-agentic-sandbox`.

**Acceptance**: verification step receives execution result context when `executionResult` is in the request.

### Task 2: Return HTTP 502 for infrastructure errors

**Problem**: Sandbox returns HTTP 200 + `success=false` for both infra errors and application failures. Temporal retry model can't distinguish them.

**Change**: ~25 lines. `_is_infrastructure_error()` checks exception types (ConnectionError, TimeoutError, APIConnectionError, RateLimitError). Infra errors → HTTP 502. Application failures → HTTP 200 + `success=false`.

**Acceptance**: spawner timeout returns 502, agent failure returns 200 with `success=false`.

## Temporal Infrastructure

### Task 3: Add `temporalio` dependency

- Add `temporalio>=1.9.0` to `pyproject.toml`
- Verify import works: `from temporalio import workflow, activity`
- Run `uv sync`

### Task 4: Temporal Server deployment manifests

**Podman** (`deploy/podman/docker-compose.temporal.yaml`):
- Temporal Server (`temporalio/auto-setup:latest`) + PostgreSQL
- Temporal Web UI (`temporalio/ui:latest`)
- Verify: `temporal operator namespace list` works

**Kind** (`deploy/kind/temporal.yaml`):
- Temporal Server Deployment + Service (port 7233)
- PostgreSQL StatefulSet (reuse existing `deploy/kind/postgres.yaml`)
- Verify: Temporal Server reachable from within cluster

### Task 5: Temporal Lite for minimal dev

- Document `temporal server start-dev` as alternative to full Temporal Server
- Single binary, SQLite storage, no PostgreSQL
- Add to dev setup instructions

## Core Implementation

### Task 6: Workflow data models

New file: `src/agents/workflow/temporal_models.py`

```python
class WorkflowInput(BaseModel):
    """Input to the generic AgentWorkflow."""
    definition: dict          # parsed workflow YAML
    input_prompt: str | None
    workflow_id: str
    provider: ProviderConfig
    sandbox_image: str
    skills_image: str | None
    skills_paths: list[str] | None

class ProviderConfig(BaseModel):
    name: str                 # claude | openai | gemini
    model: str
    credentials_secret: str

class StepResult(BaseModel):
    status: str               # completed | failed | skipped | escalated | denied
    output: dict | None
    error: str | None

class WorkflowOutput(BaseModel):
    steps: dict[str, StepResult]

class WorkflowStatus(BaseModel):
    steps: dict[str, StepResult]
    events: list[dict]

class WorkflowEvent(BaseModel):
    type: str
    step: str
    timestamp: str
```

### Task 7: AgentWorkflow Temporal class

New file: `src/agents/workflow/temporal_workflow.py`

Single generic `@workflow.defn` class that interprets any workflow YAML at runtime:
- `@workflow.run` — step loop with sequential and parallel group support
- `@workflow.signal` — `approve(step_name, decision, selected_option_id=None)` for human approval (see Approved Option Selection Contract)
- `@workflow.query` — `get_status()` returns steps + events
- Condition evaluation via existing `conditions.py` (pure, deterministic)
- Prompt interpolation via existing `interpolation.py` (pure, deterministic)
- Auto-approval via existing `auto_approve.py`
- No I/O, no `datetime.now()`, no `random()` — use `workflow.now()` for timestamps
- Parallel groups via `asyncio.gather()` on `workflow.execute_activity()` calls
- Retry via Temporal `RetryPolicy` on activities
- Escalation: catch `ActivityError` after retries exhausted → call `build_escalation_activity`

### Task 8: Sandbox activity

New file: `src/agents/workflow/temporal_activities.py`

`run_sandbox_step(input: SandboxStepInput) -> StepResult`:
1. Compute content-hash pod name from `workflow_id:step_name:attempt`
2. Build env vars: `LIGHTSPEED_PROVIDER`, `LIGHTSPEED_MODEL`
3. Build credentials: K8s via `SecretKeyRef`, Podman via host env propagation (see Credential Contract)
4. Build volume mounts: skills OCI image volume
5. Build labels: `cloud-agents/workflow-id`, `cloud-agents/step-name`, `cloud-agents/attempt` (see Crash-Boundary Contract)
6. Call `spawner.spawn()` with sandbox image, labels, and credentials
5. Call `spawner.wait_ready(endpoint, path="/health")`
6. Build sandbox request: `query`, `systemPrompt`, `outputSchema`, `context`, `timeout_ms`
7. POST to `{endpoint}/v1/agent/run`
8. If HTTP 502 → raise exception (Temporal retries)
9. If HTTP 200 + `success=false` → return `StepResult(status="failed")`
10. If HTTP 200 + `success=true` → return `StepResult(status="completed")`
11. `finally`: `spawner.destroy(pod_name)`

`build_escalation_activity(steps) -> StepResult`:
- Package workflow context for CLI handoff
- Return `StepResult(status="escalated", output=handoff_context)`

Pre-deployed agent path:
- If `step.spawn == "pre-deployed"`: skip spawn/destroy, call registry endpoint directly

### Task 9: Context building

New file or function in `temporal_activities.py`:

`build_sandbox_context(workflow_steps, current_step)`:
- Build `targetNamespaces` from step config
- Build `previousAttempts` from failed steps
- Build `approvedOption` from analysis step output (by role)
- Build `executionResult` from execution step output (by role)

Uses step `role` field (analysis | execution | verification) to find the right step output without hardcoding names. Builds `approvedOption` from the approval step's `selected_option_id` + the analysis step's options list (see Approved Option Selection Contract), not hardcoded `options[0]`.

### Task 10: Spawner changes for sandbox

Update `KubernetesSpawner._do_spawn()`:
- Accept `skills_image` and `skills_paths` parameters
- Add OCI image volume for skills (K8s 1.31+ `ImageVolume`)
- Fallback to init-container pattern for older K8s versions
- Set `LIGHTSPEED_*` env vars

Update `PodmanSpawner._do_spawn()`:
- Accept `skills_image` parameter
- Use Podman OCI image volume mount (`type=image,src=...,dst=/app/skills`)

Update `AgentSpawner.spawn()` signature to accept new parameters.

Both spawners must:
- Apply `cloud-agents/workflow-id`, `step-name`, `attempt` labels to spawned workloads
- Support label-based cleanup: `spawner.cleanup_by_labels(labels)` for orphan reconciliation
- Handle credentials per the Credential Contract:
  - K8s: `SecretKeyRef` for provider-specific env vars
  - Podman: host env propagation for provider-specific env vars

### Task 11: Temporal worker

New file: `src/agents/workflow/temporal_worker.py`

- Register `AgentWorkflow` + activities
- Configure task queue from config/env
- Set `max_concurrent_activities` from env (default: 10)
- Spawner and registry injected via activity context
- Start as sidecar container (separate from FastAPI)

### Task 12: Workflow API endpoints

Add to `src/agents/workflow/temporal_api.py` (new file):

- `POST /v1/workflows/run` — start workflow via Temporal Client
  - Parse workflow name or inline YAML
  - Resolve definition from registry
  - `temporal_client.start_workflow(AgentWorkflow.run, ...)`
  - Return 202 with workflow_id

- `POST /v1/workflows/{id}/approve` — send signal
  - Body: `{step_name, decision, selected_option_id?}` (selected_option_id required when analysis produced multiple options)
  - `handle.signal(AgentWorkflow.approve, step_name, decision, selected_option_id)`

- `GET /v1/workflows/{id}` — query workflow status
  - `handle.query(AgentWorkflow.get_status)`

- `GET /v1/workflows/{id}/events` — SSE via query polling (1s)
  - Poll `get_status`, yield new events, stop on terminal

- `POST /v1/workflows/{id}/cancel` — cancel workflow
  - `handle.cancel()`

Auth: use stack's `get_auth_dependency()` on all endpoints.

### Task 13: Entrypoint integration

Update `src/agents/workflow/entrypoint.py` or create new entrypoint:

- `WORKFLOW_ENGINE` env var: `temporal` | `legacy` (default: `legacy`)
- When `temporal`:
  - Create Temporal Client in lifespan
  - Register workflow API router
  - Start Temporal worker as sidecar (or in-process for dev)
- When `legacy`: existing behavior unchanged
- v1 endpoints always available regardless of engine

### Task 14: Workflow definition updates

Update `WorkflowStepSpec` in `src/agents/workflow/definition.py`:
- Add `runtime: Literal["sandbox", "generic"]` field (default: `sandbox`)
- Add `role: Optional[Literal["analysis", "execution", "verification"]]` field
- Add `instructions: Optional[str]` field (inline system prompt)
- Add `output_schema: Optional[dict]` field (JSON Schema)
- Add `service_account: Optional[str]` field (per-step RBAC)
- Add `target_namespaces: Optional[list[str]]` field

Update `WorkflowDefinition`:
- Add `provider: Optional[ProviderConfig]` field
- Add `skills: Optional[SkillsConfig]` field

## Testing

### Task 15: Unit tests (TDD)

Using Temporal's `WorkflowEnvironment` test harness:

- `test_sequential_workflow_completes` — 2 steps run in order
- `test_parallel_steps_run_concurrently` — steps in same `parallel_group` gather
- `test_condition_skips_step` — false condition produces "skipped" status
- `test_approval_signal_resumes_workflow` — signal unblocks wait_condition
- `test_approval_timeout_fails` — no signal within timeout → denied
- `test_retry_exhaustion_escalates` — ActivityError → escalation activity
- `test_sandbox_activity_success` — mocked spawner + HTTP → completed
- `test_sandbox_activity_502_retries` — HTTP 502 raises → Temporal retries
- `test_sandbox_activity_app_failure` — HTTP 200 + success=false → failed
- `test_context_building` — verify `build_sandbox_context` output shape
- `test_content_hash_naming` — same inputs → same pod name
- `test_pre_deployed_skips_spawn` — spawn: pre-deployed calls registry endpoint
- `test_approval_timeout_produces_denied` — no signal within timeout → denied status + event
- `test_approved_option_selection_by_id` — approval selects option by id, not hardcoded [0]
- `test_crash_after_spawn_labels_for_cleanup` — spawned pod has workflow labels for reconciliation
- `test_cancel_activity_triggers_destroy` — activity cancellation runs finally block

### Task 16: E2E tests

**Kind**:
- Deploy Temporal Server + PostgreSQL
- Deploy worker + FastAPI with `WORKFLOW_ENGINE=temporal`
- Submit 2-step workflow → sandbox pods spawn → LLM runs → results
- Verify approval signal flow
- Failure path: HTTP 502 → Temporal retry → eventual success
- Failure path: retry exhaustion → escalation → cleanup verified
- Crash boundary: label-based orphan cleanup after worker kill

**Podman**:
- `docker-compose.temporal.yaml` starts Temporal + PostgreSQL
- Worker + FastAPI containers
- Submit workflow → sandbox container → LLM → results

### Task 17: Review

Submit to independent reviewer. Address findings iteratively until LGTM.

## Dependencies

```
Task 3 (temporalio dep) ──┐
Task 4 (deploy manifests) ─┤
                            ├──→ Task 6 (models)
Task 1 (sandbox PR)        │        │
Task 2 (sandbox PR)        │        ├──→ Task 7 (workflow class)
                            │        ├──→ Task 8 (activities)
                            │        ├──→ Task 9 (context building)
                            │        │
Task 10 (spawner changes) ─┘        ├──→ Task 11 (worker)
Task 14 (definition updates) ──────┘        │
                                             ├──→ Task 12 (API)
                                             ├──→ Task 13 (entrypoint)
                                             │
                                             ├──→ Task 15 (unit tests)
                                             └──→ Task 16 (E2E tests)
                                                      │
                                                      └──→ Task 17 (review)
```

## Execution Order

1. Tasks 1, 2 (sandbox PRs — can start immediately, independent)
2. Tasks 3, 4, 5 (Temporal infra — parallel)
3. Tasks 6, 10, 14 (models + spawner + definition updates — parallel)
4. Tasks 7, 8, 9 (workflow + activities + context — depend on 6)
5. Tasks 11, 12, 13 (worker + API + entrypoint — depend on 7, 8)
6. Tasks 15, 16 (tests — depend on all above)
7. Task 17 (review)

## Verification

- `uv run pytest tests/unit/agents/ -q` — all tests pass
- `uv run make verify` — linters pass
- Temporal Server starts on both Podman and Kind
- 2-step workflow executes end-to-end with real LLM
- Approval signal pauses/resumes workflow
- v1 endpoints unaffected
