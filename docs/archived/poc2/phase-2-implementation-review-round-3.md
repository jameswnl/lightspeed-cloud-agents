# Review: Phase 2 implementation round 3 (`9c19cfe..8d9480e`)

## Findings

### 1. Major: approval notification and secret-safe notifier/escalation delivery are still not wired into the real workflow path
The core round-2 Task 4/5 gaps are still present in the current Phase 2 batch. `AgentWorkflow._handle_approval()` still never executes `send_approval_notification`, so a real paused workflow will not notify anyone. The activity itself still hardcodes `NullNotifier()`, and `build_escalation_activity()` still hardcodes `LogPackager()` plus `workflow_name="workflow"`, with no `notifier_config` / `escalation_config` ref resolution at runtime. The new integration tests register the activity, but they still never prove that the workflow actually calls it or that config refs are resolved inside the worker.

Recommended fix: call `send_approval_notification` from `_handle_approval()` with `maximum_attempts=1`, then implement runtime config-ref resolution in the worker for notifier/packager selection and add end-to-end tests that assert the workflow path, not just the helper activity.

### 2. Major: service-account and advisory enforcement still do not reach the spawner boundary
This batch adds `service_account` to `AgentSpawner.spawn()`, but the value is never forwarded into `_do_spawn()` and `run_sandbox_step()` never passes it anyway. The only current effect is still `LIGHTSPEED_SERVICE_ACCOUNT` inside sandbox env vars, which does not change the actual K8s pod identity. Advisory enforcement has the same problem: the workflow adds advisory markers and prompt suffixes, but the spawn path still does not pass a read-only ServiceAccount or a read-only Podman mode. So the trust-boundary contract from Phase 2 remains unmet even though the interface now suggests otherwise.

Recommended fix: thread the effective `service_account` / advisory execution mode through `run_sandbox_step()` into `AgentSpawner.spawn()` and the concrete spawners, then assert those values on the spawned pod/container spec in tests.

### 3. Major: Task 7 still does not implement the promised Podman skills-image behavior
The plan contract says Podman should extract the skills OCI image into a named volume and mount it into the agent container. In the current code, `run_sandbox_step()` forwards `skills_image` / `skills_paths`, `AgentSpawner.spawn()` accepts them, and `PodmanSpawner._do_spawn()` simply ignores them. The new tests only cover the Kubernetes init-container path, so the phase-wide skills-image contract is still only half implemented.

Recommended fix: implement the Podman extraction/mount flow or explicitly defer it in the plan and commit message, then add Podman-focused tests for extraction and cleanup.

### 4. Major: the committed Kind deployment assets do not match the surrounding stack and do not update the runner to Temporal
The committed `deploy/kind/temporal.yaml` uses `POSTGRES_USER=postgres` / `POSTGRES_PWD=postgres`, but the existing `deploy/kind/postgres.yaml` provisions `workflow` / `workflow-pass`. That means the committed Temporal manifest does not match the repo’s own PostgreSQL deployment. Also, Task 9 says to update the workflow-runner manifest, but `deploy/kind/workflow-runner.yaml` still launches `agents.workflow.entrypoint:app` and has no Temporal connection env vars, so the Kind deployment path is not actually switched over to the Temporal runner.

Recommended fix: make the committed Temporal manifest consistent with `deploy/kind/postgres.yaml`, and update the Kind workflow-runner deployment to use the Temporal entrypoint plus the required Temporal environment.

### 5. Medium: custom approval policy is still not reachable from the public API surface
The whole Phase 2 batch still leaves `approval_policy` as an internal-only `WorkflowInput` field. `RunWorkflowRequest` still has no typed policy field, and the definition-backed path still does not populate one, so callers cannot exercise custom auto-approval through the real API surface even though the workflow logic supports it.

Recommended fix: add `approval_policy` to the real workflow-start contract and add caller-side tests that prove it reaches `_handle_approval()`.

## Perspective Check
- Functionality: covered; important claimed Phase 2 behaviors still do not execute on the real workflow/spawner/deploy path.
- Quality: covered; new tests pass, but they still miss several caller-side and deployment seam failures.
- Security: covered; advisory/read-only enforcement is still not implemented at the actual execution boundary.

## Verification
- `git status --short`
- `git log -6 --stat --decorate`
- `git diff --name-only 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- `git diff 6c562f810a0542635f533315f7c8191c623f60bd..HEAD -- src/agents/spawner/base.py src/agents/spawner/kubernetes_spawner.py src/agents/spawner/podman_spawner.py src/agents/workflow/temporal_api.py src/agents/workflow/definition.py tests/integration/temporal/test_policy_integration.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/workflow/temporal/test_api.py deploy/kind/temporal.yaml deploy/podman/docker-compose.temporal.yaml`
- Read: `src/agents/spawner/base.py`, `src/agents/spawner/kubernetes_spawner.py`, `src/agents/spawner/podman_spawner.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/workflow/temporal_api.py`, `src/agents/workflow/definition.py`, `deploy/kind/postgres.yaml`, `deploy/kind/workflow-runner.yaml`, `deploy/kind/temporal.yaml`, `deploy/podman/docker-compose.temporal.yaml`, `tests/integration/temporal/test_policy_integration.py`, `tests/unit/agents/spawner/test_base.py`, `tests/unit/agents/spawner/test_skills_image.py`, `tests/unit/agents/workflow/temporal/test_api.py`
- `uv run pytest tests/integration/temporal/test_policy_integration.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/spawner/test_base.py -q` -> 36 passed

## Summary
This Phase 2 batch adds useful scaffolding for advisory API propagation, Kubernetes skills loading, and deployment assets, but the phase is still not review-clean. The main blockers remain the same runtime seam failures from round 2, and the committed Kind deployment path currently does not line up with the repo’s PostgreSQL credentials or the Temporal runner entrypoint.
