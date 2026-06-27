# Phase 6: Stateless Workflow Runner

## Goals & Rationale

The workflow runner must be **stateless** so it can scale horizontally behind a load balancer. Multiple replicas handle workflow requests — any replica can serve any request. This is required for production because:

1. **Scalability** — heavy workflow loads need multiple runner replicas
2. **Resilience** — pod crashes don't lose in-flight workflows
3. **No sticky sessions** — load balancer routes freely, no session affinity needed
4. **Multi-workflow** — one runner service manages many workflow definitions, submitted via API

### Current problems (from exploration)

| Problem | Current | Fix |
|---------|---------|-----|
| `_states` dict in memory | Workflow state only in the pod that created it | All state in PostgreSQL, query on every request |
| `_paused_at` dict in memory | Resume index lost on pod restart | Persist `paused_step_index` in WorkflowState |
| Single workflow.yaml per runner | Deploy new pod per workflow | Workflow definition API: submit/list/run |
| Sync agent dispatch blocks runner | `RemoteAgentClient.run()` blocks until agent responds | Async dispatch: spawn pod, record step as "running", poll or callback for result |
| SSE event queue in memory | Events lost on disconnect | **Deferred** — resumable SSE is out of scope for Phase 6. Current SSE works within a single connection. Persisted event replay is a backlog item. |

### Architecture change

```
BEFORE (stateful):
  Runner pod holds _states + _paused_at in memory
  Runner blocks on RemoteAgentClient.run()
  One workflow.yaml per runner pod

AFTER (stateless):
  Runner pods are workers behind a Service/LB
  All state in PostgreSQL
  Workflow definitions submitted via API, stored in DB
  Steps dispatched async — runner writes "dispatched" to DB,
  spawns pod, pod POSTs result to trusted runner ingest API,
  runner persists result to DB and advances workflow
  Any runner replica can advance the workflow
```

---

## Task Breakdown

### Task 1: Persist paused_step_index in WorkflowState

Add `paused_step_index: Optional[int]` to `WorkflowState`. The executor stores it on pause and reads it on resume — no more `_paused_at` dict.

**Files:**
- Modify: `src/agents/workflow/state.py` — add field
- Modify: `src/agents/workflow/executor.py` — use `state.paused_step_index` instead of `self._paused_at`
- Modify: `src/agents/workflow/postgres_persistence.py` — persist the new field
- Update: tests

---

### Task 2: Remove in-memory state — query persistence on every request

Replace `self._states` dict with persistence lookups. Every `get_state()`, `resume()`, and `list_workflows()` call hits the persistence backend.

**Files:**
- Modify: `src/agents/workflow/executor.py` — remove `_states` dict, use `self._persistence.load()` / `self._persistence.list()`
- Modify: `src/agents/workflow/persistence.py` — add `load(workflow_id)` and `list_all()` methods
- Modify: `src/agents/workflow/postgres_persistence.py` — implement load/list
- Update: tests

---

### Task 3: Workflow Definition API — submit, list, run by name

Replace file-based workflow loading with an HTTP API. Definitions stored in the persistence backend.

**Endpoints:**
- `POST /v1/workflows/definitions` — submit a workflow YAML (creates a new version)
- `GET /v1/workflows/definitions` — list stored definitions
- `GET /v1/workflows/definitions/{name}` — get latest version of a definition
- `DELETE /v1/workflows/definitions/{name}` — soft-delete (blocked if active runs reference it)
- `POST /v1/workflows/run` — takes `{"workflow_name": "..."}`, binds run to an **immutable snapshot** of the current definition version

**Definition versioning:**
- Each `POST /v1/workflows/definitions` creates a new version (auto-incrementing)
- When a workflow run is created, it stores `definition_version` — a snapshot of the definition at that point
- The run uses the snapshot for all subsequent steps, retries, and resume operations — definition changes after run creation do not affect in-flight runs
- `DELETE` is blocked if any active run references the definition; only removes future availability

**Files:**
- Create: `src/agents/workflow/definition_store.py` — CRUD with versioning for workflow definitions in DB
- Modify: `src/agents/workflow/api.py` — add definition endpoints, update run endpoint
- Modify: `src/agents/workflow/state.py` — add `definition_version` and `definition_snapshot` to WorkflowState
- Modify: `src/agents/workflow/entrypoint.py` — no longer loads single workflow.yaml (optional bootstrap)
- Update: tests

---

### Task 4: Async step dispatch with DB-backed results

Replace the synchronous `RemoteAgentClient.run()` pattern with async dispatch. **Result-ingest API approach** (from reviewer): ephemeral pods never get DB credentials. They POST results to a trusted runner endpoint. The runner writes to DB and advances the workflow.

**Security principle:** Ephemeral agent pods are untrusted workloads. They receive only: the prompt, an API token, and a result callback URL. They never get direct database access.

**Leverages existing infrastructure:** The agent runtime already supports async runs via `run_async()` + `poll_run()` + `RunStore`. The spawner passes `RESULT_CALLBACK_URL`, `STEP_ID`, and `ATTEMPT_ID` env vars.

**Flow:**
1. Runner creates step record in DB with status `"dispatched"`, assigns unique `step_id` + `attempt_id`
2. Runner spawns ephemeral pod with env: `STEP_ID`, `ATTEMPT_ID`, `RESULT_CALLBACK_URL`, `AGENT_API_TOKEN`
3. Runner returns immediately (non-blocking)
4. Ephemeral pod runs the agent, POSTs result to `RESULT_CALLBACK_URL` (authenticated via token)
5. Runner replica receiving callback validates `step_id`/`attempt_id`, writes to DB, advances workflow
6. If callback is lost (pod crash, network issue), recovery poller detects orphaned step and retries
7. Pod cleanup: runner destroys pod after result is persisted

**Idempotency contract:**
- Each dispatch creates a unique `attempt_id` (UUID)
- Only the first completion for a given `attempt_id` is accepted; duplicates are ignored
- If a step is retried (new attempt), the old `attempt_id` becomes stale — completions for stale attempts are rejected
- Terminal states (`completed`, `failed`) are immutable — no further transitions allowed

**Files:**
- Create: `src/agents/workflow/step_dispatcher.py` — async dispatch logic
- Modify: `src/agents/workflow/executor.py` — use dispatcher instead of direct `client.run()`
- Modify: `src/agents/workflow/state.py` — add `"dispatched"` status, `step_id`, `attempt_id` to StepResult
- Modify: `src/agents/workflow/api.py` — add `POST /v1/workflows/steps/{step_id}/complete` result-ingest endpoint with token auth
- Modify: agent runtime — POST result to callback URL after run completes
- Modify: spawner — pass callback URL + step ID + attempt ID to spawned pods (NO DB credentials)
- Update: tests

---

### Task 5: Workflow advancement + recovery poller

**Primary path:** Callback-triggered advancement. When the runner receives the "step done" callback, it loads the workflow from DB, advances to the next step (dispatch next or complete).

**Recovery path:** Background poller on every replica checks for orphaned steps (status `"dispatched"` for longer than timeout) and either retries or marks failed.

**Optimistic locking:** `UPDATE workflow_runs SET version = version + 1 WHERE workflow_id = ? AND version = ?`. Raise `StaleStateError` on conflict, retry. Prevents two replicas advancing the same workflow simultaneously.

**Files:**
- Create: `src/agents/workflow/advancement.py` — callback-triggered advancement + recovery poller
- Modify: `src/agents/workflow/state.py` — add `version: int` for optimistic locking
- Modify: `src/agents/workflow/persistence.py` — add `save_with_version()`, `claim_advanceable()` with CAS
- Modify: `src/agents/workflow/entrypoint.py` — start recovery poller as background task
- Update: tests

---

### Task 6: Deprecate GraphExecutor

GraphExecutor has the same stateful problems (`_states`, `_paused_at` in memory) and was already scoped as "same-process exploratory only, cannot survive restarts". Remove the executor selection path — keep the code for reference but remove from the entrypoint. The assessment document already recommends keeping WorkflowExecutor as production.

**Files:**
- Modify: `src/agents/workflow/entrypoint.py` — remove `WORKFLOW_EXECUTOR=graph` path
- Keep: `src/agents/workflow/graph_executor.py` — but mark as deprecated/exploratory
- Update: tests

---

### Task 7: Stateless entrypoint + multi-replica deployment

Update the entrypoint and K8s manifests for stateless multi-replica deployment.

**Files:**
- Modify: `src/agents/workflow/entrypoint.py` — remove file-based workflow loading, start recovery poller
- Modify: `deploy/kind/workflow-runner.yaml` — `replicas: 2`, remove workflow.yaml ConfigMap mount
- Update: tests + E2E verification with 2 replicas

**E2E test:**
1. Deploy workflow-runner with `replicas: 2` in Kind
2. Submit a workflow definition via API
3. Start a workflow run, verify step dispatched
4. Kill one runner replica
5. Verify the other replica picks up and completes the workflow via recovery poller

---

## Task Dependencies

```
Task 1 (persist pause index)  ──┐
Task 2 (remove _states dict)  ──┤
Task 3 (definition API)       ──┤──→ Task 4 (async dispatch) → Task 5 (advancement + poller) → Task 6 (deprecate GraphExecutor) → Task 7 (stateless entrypoint + E2E)
                                │
Note: PostgreSQL schema changes are incremental within each task, not a separate task.
```

Tasks 1-3 can be developed in parallel. Tasks 4-5 are sequential. Task 6 is cleanup. Task 7 is integration + E2E.

---

## Verification

```bash
uv run pytest tests/unit/agents/ -q                   # unit tests
uv run pytest examples/tests/ -q                      # example tests
```

**E2E (2-replica stateless test):**
1. Deploy workflow-runner with `replicas: 2` in Kind
2. Submit a workflow definition via API
3. Start a workflow run, verify step dispatched
4. Kill one runner replica mid-execution
5. Verify the other replica picks up and completes via recovery poller
6. Verify approval pause/resume works across replicas (pause on replica A, resume on replica B)

**Concurrency/failure test cases (unit):**
- Duplicate callback: same `step_id`/`attempt_id` submitted twice → second is ignored
- Stale callback: old `attempt_id` submitted after retry created new attempt → rejected
- Optimistic lock conflict: two replicas try to advance same workflow → one succeeds, other retries
- Lost callback: step stays `"dispatched"` past timeout → recovery poller marks failed or retries
- Definition update during active run: run continues with original snapshot, not updated definition
- Definition delete with active run: delete is blocked with error
- Approval resume on different replica: works because state is loaded from DB
