# Review: Phase 2 full commit range (`9c19cfe..1e2ec15`)

## Findings

### 1. Major: approval notification and config-ref delivery still are not wired into the real workflow path
Across the full Phase 2 range, `AgentWorkflow._handle_approval()` still never executes `send_approval_notification()`, so a real paused workflow will not notify anyone. The helper activity exists, but it is never called from the live workflow path. The same applies to delivery configuration: `send_approval_notification()` still hardcodes `NullNotifier()`, and `build_escalation_activity()` still hardcodes `LogPackager()` plus `workflow_name="workflow"`. That means the Task 4/5 contract for worker-side `notifier_config` / `escalation_config` reference resolution is still not implemented.

Recommended fix: invoke `send_approval_notification` from `_handle_approval()` with `maximum_attempts=1`, then resolve notifier/packager config refs inside the worker process and add workflow-level tests that prove those runtime paths execute.

### 2. Major: service-account and advisory enforcement still do not reach the execution boundary
The Phase 2 design requires enforcement at spawn time, not only via prompt/output annotations. In the current full batch, `run_sandbox_step()` still only copies `permissions.service_account` into `LIGHTSPEED_SERVICE_ACCOUNT` inside sandbox env vars. It still does not pass `service_account` into `spawner.spawn()`, and `AgentSpawner.spawn()` still drops its `service_account` argument before `_do_spawn()`. Advisory mode has the same problem: the workflow marks prompts and outputs, but it still does not pass a read-only ServiceAccount or a read-only Podman execution mode into the spawner layer. So the trust-boundary contract remains unmet for the entire Phase 2 implementation.

Recommended fix: thread effective `service_account` and advisory read-only mode through `run_sandbox_step()` into the concrete spawners, then assert on the spawned pod/container spec rather than only on workflow outputs or env vars.

### 3. Major: Podman skills-image support is still only a passthrough, not an implementation
Task 7’s Podman side is still incomplete over the full commit range. `skills_image` / `skills_paths` are forwarded through the workflow, activity, and spawner interface layers, but `PodmanSpawner._do_spawn()` still ignores them completely. The existing tests only prove the Kubernetes init-container path, so the claimed cross-runtime skills-image contract is still only half implemented.

Recommended fix: implement the named-volume extraction/mount flow for Podman or explicitly defer/narrow that deliverable, then add Podman-specific tests for extraction and cleanup.

### 4. Major: the Kind workflow-runner manifest still is not switched to the Temporal runner
The later Phase 2 commits fixed the specific Temporal/Postgres credential mismatch in `deploy/kind/temporal.yaml`, but `deploy/kind/workflow-runner.yaml` still launches `agents.workflow.entrypoint:app` instead of the Temporal runner entrypoint and still lacks the Temporal connection environment used by the Podman Temporal stack. So Task 9 remains incomplete in the committed Kind deployment assets even after the later infra fixes.

Recommended fix: update `deploy/kind/workflow-runner.yaml` to use the Temporal entrypoint and matching Temporal environment configuration, then verify the committed Kind manifest set from manifests alone.

### 5. Medium: custom approval policy still is not reachable from the public API surface
`WorkflowInput` supports `approval_policy`, and the workflow logic uses it, but the real workflow start contract still does not expose it. `RunWorkflowRequest` has no typed policy field, and the definition-backed API path does not populate one. So custom auto-approval remains an internal/test-only capability rather than a caller-usable feature across the current Phase 2 implementation.

Recommended fix: add `approval_policy` to the workflow-start API contract and add caller-side tests that prove it reaches `_handle_approval()`.

## Perspective Check
- Functionality: covered; several claimed Phase 2 runtime behaviors still do not execute on the real workflow/spawner/deploy path.
- Quality: covered; focused verification passes, but important caller-side and deploy-seam behavior is still unproven or missing.
- Security: covered; advisory/read-only enforcement still is not implemented at the actual execution boundary.

## Verification
- `git status --short`
- `git log --reverse --stat --decorate 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- `git diff --name-only 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- `git diff 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- Read: `src/agents/workflow/temporal_workflow.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/workflow/temporal_models.py`, `src/agents/workflow/temporal_worker.py`, `src/agents/workflow/temporal_api.py`, `src/agents/workflow/notifier.py`, `src/agents/workflow/escalation.py`, `src/agents/spawner/base.py`, `src/agents/spawner/kubernetes_spawner.py`, `src/agents/spawner/podman_spawner.py`, `deploy/kind/temporal.yaml`, `deploy/kind/temporal-lite.yaml`, `deploy/kind/workflow-runner.yaml`, `deploy/podman/docker-compose.temporal.yaml`, `tests/integration/temporal/test_policy_integration.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/unit/agents/workflow/temporal/test_activities.py`, `tests/unit/agents/workflow/temporal/test_workflow.py`, `tests/unit/agents/spawner/test_skills_image.py`, `tests/unit/agents/spawner/test_base.py`
- `uv run pytest tests/integration/temporal/test_policy_integration.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/spawner/test_base.py -q` -> 49 passed

## Summary
The full Phase 2 commit range is still not review-clean. The later commits improved the infrastructure story and fixed the Kind Temporal/Postgres mismatch, but the main blockers remain the same high-signal runtime seam failures: notification/config-ref delivery is not wired into the workflow path, spawn-time enforcement does not match the advisory/permissions contract, Podman skills support is not implemented, and the Kind workflow-runner manifest still is not switched to the Temporal runner.
