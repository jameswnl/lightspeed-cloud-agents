# Phase 8: Production Readiness — Async Dispatch & Operational Hardening

## Context

The workflow runner currently executes steps **synchronously**: it spawns a pod, holds the HTTP connection open for up to 600s, collects the result, and destroys the pod. Persistence writes happen but are operationally redundant — the runner already has the result in memory. If the runner crashes mid-step, the result is lost and the recovery poller can only mark the step failed and retry from scratch.

This phase makes persistence **load-bearing**: the runner fires off a step, persists "dispatched" status, and returns control. The step pod POSTs its result back via a callback. An advancement engine processes the result and dispatches the next step. The recovery poller serves as a fallback for failed callbacks.

## Async Runtime Contracts

### Attempt Identity

Every dispatched step is uniquely identified by `(workflow_id, step_name, attempt)`. The `attempt` counter (1-indexed) is persisted in `StepResult` and included in all callback paths. This ensures:

- Duplicate callbacks from the same attempt are idempotent (second POST is a no-op 200)
- Stale callbacks from a prior attempt (e.g., late callback from attempt 1 after attempt 2 is dispatched) are rejected with 409
- Terminal attempt records are immutable — once a StepResult is `completed` or `failed`, it cannot be overwritten

### Persist-Before-Spawn (Pre-Persist Crash Boundary)

The async dispatch sequence is: **persist "dispatched" → spawn pod → submit work**. Persistence happens *before* spawning, not after. This means:

- **Crash after persist, before spawn**: the step is recorded as "dispatched" with `run_id=None` but no pod exists. On recovery, the poller sees "dispatched" + `run_id=None`, tries the endpoint, finds it unreachable, and marks the step **failed** with error "dispatch interrupted — pod never spawned." This routes through `advance_workflow()` → normal retry policy (increment attempt, re-dispatch if `max_retries` not exhausted, else fail workflow with escalation). The failed attempt is terminal and immutable; the retry creates a new attempt.
- **Crash after spawn, before submit**: the pod exists and is idle. The poller sees "dispatched" + `run_id=None`, tries the endpoint, finds it reachable, re-submits async work, persists the returned `run_id` via CAS. If two replicas race to re-submit, first-writer-wins on the CAS update — the loser detects `run_id` is already set and returns early.
- **Crash after submit**: the pod is running the task. Normal recovery — poller polls `run_id`, gets result or waits for timeout.

This eliminates the "spawned but not persisted" gap: if a "dispatched" record doesn't exist in state, the spawn never happened (or the persist failed and was rolled back). Orphaned pods with no matching state record are cleaned up by the label-based reconciliation scan (see below).

### Label-Based Orphan Reconciliation

As a secondary safety net, the recovery poller periodically lists spawned resources by label (`cloud-agents/workflow-id`) and cross-references against persisted workflow state. Resources with no matching "dispatched" step (e.g., spawned after persist but before the crash rolled back the persist) are destroyed. This handles the narrow edge case where a spawn succeeded but the preceding persist was not durable (e.g., network partition to the database after the write appeared to succeed).

### Durable Recovery Handle

When `dispatch_async()` persists a "dispatched" StepResult, the `output` dict contains the **recovery contract** — everything another replica needs to poll or clean up the step after a crash:

```python
output = {
    "spawned_name": "diagnostic-a3f5c2d9",    # reconstructible from workflow_id:step_name:attempt
    "run_id": "uuid-from-agent-runtime",       # async run handle for GET /v1/runs/{run_id} (None until submitted)
    "endpoint": "http://agent-diagnostic-a3f5c2d9.default.svc:8080",  # stable Service DNS
    "attempt": 1,                               # current attempt number
}
```

`run_id` is initially `None` when persisted before spawn. After successful async submission, `dispatch_async()` updates the step output with the `run_id` and re-persists. The recovery poller handles both cases: with `run_id` (poll for result), and without (re-submit if pod is reachable, otherwise mark the attempt failed and route through normal retry policy).

The recovery poller on any replica can:
1. Read `endpoint` + `run_id` from persisted step output
2. If `run_id` is set: poll `GET /v1/runs/{run_id}` to check if the agent finished. Ingest via `ingest_step_result()` then `advance_workflow()`.
3. If `run_id` is None and pod is reachable: re-submit async, persist returned `run_id` via CAS.
4. If `run_id` is None and pod is not reachable: mark step **failed** ("dispatch interrupted — pod never spawned"), call `ingest_step_result()` then `advance_workflow()` so normal retry policy decides the next action.
5. Call `spawner.destroy(spawned_name)` to clean up after completed/failed steps.

Agent runtime retains run results in `RunStore` until the pod is destroyed (TTL or explicit cleanup). This gives the recovery poller a window to retrieve results after a missed callback.

### Persistence Ownership

`dispatch_async()` is the **sole owner** of persisting "dispatched" state. It writes the StepResult internally before spawning, and updates it with `run_id` after async submission. Callers do not persist the dispatched state — they call `dispatch_async()` and it handles persistence.

Result ingestion follows a two-step pipeline shared by both the callback endpoint and the recovery poller:
1. **`ingest_step_result(persistence, workflow_id, step_name, payload)`** — persists the completed/failed attempt with CAS and idempotency rules (same attempt + same status = no-op, stale attempt = reject, terminal record = immutable). This is a standalone function, not part of `advance_workflow()`.
2. **`advance_workflow(persistence, dispatcher, workflow_id)`** — runs only after `ingest_step_result()` succeeds. Evaluates the next step and dispatches it.

Both the ingest endpoint and the recovery poller call `ingest_step_result()` followed by `advance_workflow()`. This ensures recovered results go through the same idempotency and CAS rules as callbacks.

### Multi-Replica Advancement Safety

All state mutations use `save_with_version()` (CAS). The advancement contract:

1. **First-writer-wins**: when two replicas race to advance the same workflow (callback vs callback, or callback vs poller), the first to increment the version wins. The loser gets `StaleStateError`, reloads state, and checks if the step was already advanced.
2. **Attempt claiming**: before dispatching the next step, `advance_workflow()` verifies the step it's about to dispatch is still in `pending` status. If another replica already set it to `dispatched`, the loser returns early.
3. **Retry loop**: `advance_workflow()` retries up to 3 times on `StaleStateError` (reload → re-check → re-advance). After 3 failures, it logs and exits — the recovery poller will catch it.

### K8s Trust Boundary

Phase 8 delivers **audience-scoped TokenReview authentication** for Kubernetes. Pods authenticate via projected SA tokens validated by the K8s API server (audience: `cloud-agents`). This replaces the shared-secret model with per-pod cryptographic tokens.

**What Phase 8 delivers**: any pod with a valid projected SA token for the `cloud-agents` audience can authenticate to both the agent runtime and the workflow runner's ingest endpoint. This is a significant improvement over shared secrets — tokens are short-lived (3600s), cryptographically signed, and validated by the K8s API server.

**What is deferred**: per-job identity binding — verifying that the specific spawned Job/attempt is the one making the callback. This requires generating per-job ServiceAccounts at spawn time. See BACKLOG.md.

For Podman, shared-secret auth (`AGENT_API_TOKEN`) remains the production model.

## Tasks

### Task 1: Result-Ingest API Endpoint

New endpoint `POST /v1/workflows/{workflow_id}/steps/{step_name}/result` that receives completed step results from agent pods.

**Request model** (`StepResultPayload`):
```python
class StepResultPayload(BaseModel):
    status: Literal["completed", "failed"]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    completed_at: str
    attempt: int  # which attempt this callback is for
```

**Behavior**: the endpoint calls the two-step pipeline:
1. `ingest_step_result(persistence, workflow_id, step_name, payload)` — persists the result with CAS and idempotency rules
2. `advance_workflow(persistence, dispatcher, workflow_id)` — evaluates and dispatches the next step

`ingest_step_result()` is a standalone function (not part of the endpoint handler) so the recovery poller can reuse it:
1. Load workflow state from persistence
2. Validate step exists and has a StepResult in "dispatched" or "pending" status
3. Validate `attempt` matches the current attempt in the step's persisted output — reject if stale (prior attempt) or if step is already terminal (completed/failed)
4. Update StepResult with payload
5. `save_with_version()` for CAS (prevents duplicate ingest across replicas)
6. On duplicate callback for same attempt + same terminal status: return success (idempotent)

The endpoint returns 200 on success, 409 on stale/duplicate attempt, 404 on unknown workflow/step.

**Auth**: Covered by existing `BearerAuthMiddleware` — NOT added to EXEMPT_PATHS.

### Task 2: Async Dispatch in StepDispatcher

Add `dispatch_async()` to `StepDispatcher`. `dispatch_async()` is the **sole owner** of persisting "dispatched" state — callers do not persist separately.

**Sequence** (persist-before-spawn):
1. Build StepResult with `status="dispatched"`, `output={spawned_name, run_id=None, endpoint, attempt}`
2. **Persist** "dispatched" state via `save_with_version()` (CAS) — this is the crash boundary
3. Spawn pod (idempotent via Task 7 if crash-retried)
4. Wait for readiness
5. Call `client.run_async()` → get `run_id`
6. **Update** step output with `run_id`, re-persist
7. Return the StepResult
8. **Do NOT destroy pod** — it stays alive to complete the task
9. **Do NOT block** on result

**Durable recovery handle**: the `output` dict contains all fields from the recovery contract. `spawned_name` is reconstructible from `workflow_id:step_name:attempt` via content-hash (Phase 7 pattern). `endpoint` is the stable Service DNS. `run_id` is set after async submission (initially None).

Pass callback URL + auth as env vars to spawned pod:
- `RESULT_CALLBACK_URL=http://workflow-runner.{ns}.svc:8080/v1/workflows/{wf_id}/steps/{step_name}/result`
- `AGENT_API_TOKEN` — so the pod can auth to the callback

Pre-deployed agents remain synchronous (no change).

### Task 3: Workflow Advancement Engine

New function `advance_workflow(persistence, dispatcher, workflow_id)` in `advancement.py`. This runs **only after** `ingest_step_result()` has durably stored the completed step. It does not persist results — only reads them and dispatches the next step.

**CAS contract**:
- All state mutations via `save_with_version()` with retry loop (max 3 attempts on `StaleStateError`)
- Before dispatching next step: verify it's still `pending` (attempt claiming — prevents double-dispatch)
- On `StaleStateError`: reload state, check if step already advanced, return early if so
- Callback-vs-callback and callback-vs-poller races both resolve via first-writer-wins CAS

**Behavior**:
1. Load workflow state (fresh read — step result already persisted by `ingest_step_result()`)
2. Find the next step to execute
3. Evaluate condition; if false, skip
4. If next step is agent + ephemeral: call `dispatch_async()` (which persists "dispatched" internally)
5. If next step is human-approval: set "awaiting_approval", persist with CAS
6. If all steps done: workflow "completed"
7. If failed step: check retry budget, re-dispatch (incrementing attempt) or fail workflow

Must be **idempotent**: second call for same workflow state detects step already advanced, returns early.

**Depends on**: Tasks 1, 2

### Task 4: Agent Runtime Callback on Completion

Modify `_run_in_background()` in `server.py`: after `store.complete_run()` or `store.fail_run()`, check `RESULT_CALLBACK_URL` env var. If set, POST result to that URL.

New module `src/agents/runtime/callback.py` with `ResultCallback`:
- Retry up to 3 times with exponential backoff (1s, 2s, 4s)
- Include `Authorization: Bearer {AGENT_API_TOKEN}` header
- Include `attempt` field from env var `RESULT_CALLBACK_ATTEMPT`
- Log but don't crash on failure (recovery poller is the safety net)
- Agent runtime retains run results in `RunStore` until pod shutdown — the recovery poller's poll window

**Depends on**: Task 1 (payload schema)

### Task 5: Recovery Poller Enhancement — Result Recovery

Upgrade `RecoveryPoller._poll_once()` to attempt result recovery before marking failed.

**Durable recovery contract**: uses the persisted recovery handle from Task 2's dispatched StepResult:
1. Read `endpoint`, `run_id`, `spawned_name`, `attempt` from step's `output` dict

**When `run_id` is set** (normal async dispatch completed):
2. Try `RemoteAgentClient(endpoint).poll_run(run_id)` — agent runtime retains results until pod destruction
3. If completed: call `ingest_step_result()` then `advance_workflow()` (same two-step pipeline as the callback endpoint)
4. If failed: call `ingest_step_result()` with failure, then `advance_workflow()`
5. If running + past timeout: mark step failed ("timed out"), call `ingest_step_result()` + `advance_workflow()` (retry policy decides next action)
6. If pod unreachable: mark step failed ("pod unreachable"), call `ingest_step_result()` + `advance_workflow()`

**When `run_id` is None** (crash before async submission):
7. If pod is reachable: re-submit async via `client.run_async()`, persist returned `run_id` via CAS. If CAS fails (another replica already re-submitted), reload and return early.
8. If pod is not reachable: mark step **failed** ("dispatch interrupted — pod never spawned"), call `ingest_step_result()` + `advance_workflow()`. The retry policy in `advance_workflow()` will re-dispatch a new attempt if `max_retries` allows.

**Cleanup**: after handling completed/failed steps, call `spawner.destroy(spawned_name)`.

**CAS safety**: all poller state mutations use `save_with_version()`. Two replicas racing on the same step: first-writer-wins, loser reloads and returns early.

**Label-based reconciliation** (secondary safety net): list spawned resources by label `cloud-agents/workflow-id`, cross-reference against persisted state. Resources with no matching "dispatched" step are destroyed.

RecoveryPoller constructor gains `client_factory` and `dispatcher` parameters.

**Depends on**: Tasks 2, 3

### Task 6: Executor Dual-Mode Integration

Wire async dispatch into `WorkflowExecutor`:
- New constructor param: `callback_base_url: str = ""`
- In `_execute_from()`: if step is ephemeral + `callback_base_url` is set → use `dispatch_async()` and return (workflow stays "running")
- If no `callback_base_url` or pre-deployed → existing sync path (unchanged)
- In `entrypoint.py`: read `CALLBACK_BASE_URL` from env, pass to executor and dispatcher

The key behavioral change: with async mode, `_execute_from()` dispatches **one step** and returns. Advancement (Task 3) drives the next step when the callback arrives.

**Depends on**: Tasks 2, 3

### Task 7: AlreadyExists Idempotent Job/Container Creation

**Reconstructible naming**: spawned resource names are computed from `workflow_id:step_name:attempt` via SHA256 content-hash (Phase 7 pattern). These three fields are persisted in step output. This makes retry idempotency and crash cleanup work because any replica can reconstruct the expected resource name from persisted workflow state.

**KubernetesSpawner** (`_do_spawn`):
- Catch `ApiException(status=409)` on Job creation
- Verify existing Job has same image; if mismatch, raise
- Same for Service creation
- On 409 match: log and return existing endpoint (idempotent)

**PodmanSpawner**:
- Check if container with same name exists and is running → reuse
- If exists but stopped → remove and recreate

**Depends on**: Nothing (independent)

### Task 8: Workflow Visibility Labels

Add labels to spawned Jobs/containers:
```
cloud-agents/workflow-id: {workflow_id}
cloud-agents/step-name: {step_name}
cloud-agents/attempt: {attempt}
cloud-agents/created-at: {timestamp}
```

- `AgentSpawner.spawn()` gains optional `labels: dict[str, str]` parameter
- Both spawners apply labels to resources
- Executor/dispatcher passes workflow context when spawning

**Depends on**: Nothing (independent)

### Task 9: Multi-Replica E2E with PostgreSQL

Infrastructure smoke test verifying multi-replica deployment with PostgreSQL persistence.

**Infrastructure**: Kind cluster, 2 workflow-runner replicas, PostgreSQL StatefulSet.

**Shipped test coverage** (infrastructure verification):
1. **Cluster bring-up**: PostgreSQL ready, 2 runner replicas ready
2. **Cross-replica healthz**: both replicas respond to `/healthz`
3. **Ingest endpoint validation**: 404 for unknown workflow, 422 for invalid payload
4. **Auth enforcement**: 401 without bearer token on ingest endpoint
5. **Label query**: `kubectl get jobs -l cloud-agents/workflow-id` selector works

**Deferred runtime scenarios** (require running LLM + real workflow execution):
- Happy path workflow completion via callbacks
- Duplicate/stale callback handling
- Lost-callback poller recovery
- Crash-after-persist replica failover
- Orphaned resource reconciliation

**Depends on**: All prior tasks

### Task 10: K8s Per-Pod Identity via TokenReview

**Scope**: required before K8s production rollout of async callbacks. Until completed, K8s callback mode uses shared-secret auth (same as Podman). Podman callback mode is production-ready without this task.

**Implemented scope**: audience-scoped TokenReview authentication. Any pod with a valid projected SA token for audience `cloud-agents` can authenticate. This replaces the shared-secret model with per-pod cryptographic tokens validated by the K8s API server.

- New `TokenReviewAuthMiddleware` in `src/agents/runtime/auth.py`
- Projected SA token volume on spawned Jobs and workflow runner (audience: `cloud-agents`, expiry: 3600s)
- `automountServiceAccountToken: false` on spawned pods
- Auth mode selection via `AUTH_MODE` env var (`shared_secret` | `sa_token`)
- RBAC: `ClusterRole` granting `authentication.k8s.io/tokenreviews` create permission
- `get_runner_auth_token()` reads projected token on both runner and callback sides

**Deferred to backlog**: per-job identity binding — generating per-job ServiceAccounts at spawn time and verifying the TokenReview caller identity matches the specific spawned Job/attempt. This is a deeper change (dynamic SA creation, RBAC per SA, identity-to-step mapping in the ingest endpoint).

**Depends on**: Nothing code-wise, but is a prerequisite for K8s production callback mode

## Dependency Graph

```
Task 1 (Ingest Endpoint) ──┐
                             ├──→ Task 3 (Advancement) ──→ Task 5 (Recovery Enhancement)
Task 2 (Async Dispatch)  ──┘         │                           │
                                      ├──→ Task 6 (Dual-Mode) ──┤
Task 4 (Agent Callback) ─── needs 1  │                           │
                                      │                           ▼
Task 7 (AlreadyExists) ── independent │                    Task 9 (Multi-Replica E2E)
Task 8 (Visibility Labels) ── independent
Task 10 (TokenReview) ── independent code-wise; prerequisite for K8s production callback mode
```

## Execution Order

1. Tasks 1, 2, 7, 8 in parallel (no dependencies)
2. Tasks 3, 4 (depend on 1 and 2)
3. Tasks 5, 6 (depend on 2, 3)
4. Task 9 (integration capstone)
5. Task 10 (K8s production gate)
