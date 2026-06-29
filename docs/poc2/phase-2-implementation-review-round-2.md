# Review: Phase 2 implementation round 2 (`9c19cfe..6c562f8`)

## Findings

### 1. Major: approval notifications are implemented as a standalone activity but never triggered by the workflow
Task 4 claims approval pauses notify operators, but `AgentWorkflow._handle_approval()` only emits `workflow.paused` and waits for a signal or timeout. There is no `workflow.execute_activity("send_approval_notification", ...)` call anywhere in the workflow code, so real paused workflows will never send anything. The tests only call `send_approval_notification()` directly, which is why this slipped through while the suite stayed green.

Recommended fix: invoke the notification activity from `_handle_approval()` when the workflow pauses, and set the activity retry policy to `maximum_attempts=1` so the runtime behavior matches the "best-effort with possible duplicates" contract.

### 2. Major: advisory mode and permission scope still do not reach the spawner trust boundary
The Phase 2 contract requires advisory execution to be enforced at spawn time: K8s should use a read-only ServiceAccount and Podman should run read-only without host mounts. The current implementation does not do that. `run_sandbox_step()` writes `permissions.service_account` into `LIGHTSPEED_SERVICE_ACCOUNT` inside the sandbox env, but the actual K8s ServiceAccount is fixed by the spawner instance (`KubernetesSpawner._service_account`) and is not overridable per spawn. Likewise, the workflow sets `resolved_step["advisory"] = True`, but `run_sandbox_step()` never reads that flag and the Podman spawner still mounts its configured host volumes normally.

Recommended fix: extend the spawner contract so per-step spawn calls can carry the effective ServiceAccount / read-only mode, then add tests that assert the spawner sees those values rather than only checking workflow output markers.

### 3. Major: Task 4 and Task 5 secret-safe config-ref delivery paths are not implemented
The plan says `notifier_config` and `escalation_config` are references resolved at runtime to real notifier/packager credentials. The implementation ignores those inputs entirely. `send_approval_notification()` always instantiates `NullNotifier()`, and `build_escalation_activity()` always instantiates `LogPackager()` with a hardcoded `workflow_name="workflow"`. There is no config-ref resolution, no notifier/packager selection, and no proof that secrets stay out of Temporal payloads because the real delivery path is not wired yet.

Recommended fix: accept config refs in the activity inputs, resolve them from env/K8s secrets inside the worker process, construct the matching notifier/packager implementation there, and add tests for both resolution and delivery selection.

### 4. Medium: the round-1 `approval_policy` propagation gap is still unresolved
Round 1 flagged that custom auto-approval policy only worked in unit tests that mutate `WorkflowInput` directly. That is still true in this commit: `WorkflowInput` can carry `approval_policy`, but the actual workflow entrypoints still do not accept or populate it. `RunWorkflowRequest` has no `approval_policy` field and the workflow-definition path does not carry one either, so callers still cannot exercise custom policy through the real API surface.

Recommended fix: plumb a typed approval-policy field through the real workflow start path and add caller-side tests that prove it reaches `_handle_approval()`.

## Perspective Check
- Functionality: covered; found multiple behavior gaps where the committed code does not deliver the claimed Task 2-5 runtime behavior.
- Quality: covered; focused tests pass, but several of them validate helper activities directly instead of proving the full workflow contract.
- Security: covered; advisory enforcement and secret-safe config resolution remain incomplete at the actual trust boundaries.

## Verification
- `git status --short`
- `git log -5 --stat --decorate`
- `git diff --name-only 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- Read: `src/agents/workflow/auto_approve.py`, `src/agents/workflow/advisory.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/workflow/temporal_models.py`, `src/agents/workflow/temporal_worker.py`, `src/agents/workflow/temporal_workflow.py`, `src/agents/workflow/notifier.py`, `src/agents/workflow/escalation.py`, `src/agents/spawner/base.py`, `src/agents/spawner/kubernetes_spawner.py`, `src/agents/spawner/podman_spawner.py`, `tests/unit/agents/workflow/temporal/test_activities.py`, `tests/unit/agents/workflow/temporal/test_workflow.py`, `tests/unit/agents/workflow/temporal/test_api.py`
- `uv run pytest tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_api.py -q` -> 35 passed

## Summary
This commit moves Phase 2 forward, but it is not review-clean yet. The highest-risk gap is that several contracts are only represented by helper code or output markers, while the real workflow/spawner path still does not send notifications, enforce advisory execution at spawn time, or resolve notifier/escalation config refs. Polling remains active for the next settled Phase 2 commit.
