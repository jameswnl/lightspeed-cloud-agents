# Review: Phase 2 implementation round 4 (`9c19cfe..b10b994`)

## Findings

### 1. Major: approval notification and config-ref delivery are still not wired into the live workflow path
The latest commit only adjusts Kind deployment manifests; it does not address the core Task 4/5 runtime gaps. `AgentWorkflow._handle_approval()` still never executes `send_approval_notification()`, so real paused workflows still do not notify anyone. The activity still hardcodes `NullNotifier()`, and `build_escalation_activity()` still hardcodes `LogPackager()` with `workflow_name="workflow"`, so the secret-safe `notifier_config` / `escalation_config` reference contract is still unimplemented.

Recommended fix: invoke `send_approval_notification` from `_handle_approval()` with a single-attempt retry policy, then implement runtime config-ref resolution inside the worker for notifier/packager selection and add workflow-level tests that prove those paths execute.

### 2. Major: service-account and advisory enforcement still do not reach the spawner boundary
The actual execution-boundary problem from round 3 is unchanged. `run_sandbox_step()` still only writes `permissions.service_account` into `LIGHTSPEED_SERVICE_ACCOUNT` inside sandbox env vars, and still does not pass `service_account` or any read-only/advisory execution mode into `spawner.spawn()`. `AgentSpawner.spawn()` exposes a `service_account` parameter now, but it is still dropped before `_do_spawn()`. That means the K8s pod identity and Podman execution mode are still not controlled by the workflow’s advisory/permission inputs.

Recommended fix: thread the effective `service_account` and advisory read-only mode through `run_sandbox_step()` into the concrete spawners, then assert on the spawned pod/container spec instead of only on workflow outputs or env vars.

### 3. Major: Podman skills-image support is still not implemented
Task 7 remains only half done. `skills_image` / `skills_paths` are forwarded through the workflow and activity layers, but `PodmanSpawner._do_spawn()` still ignores them completely. The new Kind/E2E commit does not change that, and the automated coverage still only proves the Kubernetes init-container path.

Recommended fix: implement the named-volume extraction/mount flow for Podman or explicitly defer it and narrow the Phase 2 deliverable, then add Podman-focused tests for extraction and cleanup.

### 4. Major: the Kind workflow-runner deployment still has not been switched to the Temporal entrypoint
The latest commit fixes the Temporal server manifest, but `deploy/kind/workflow-runner.yaml` still launches `agents.workflow.entrypoint:app` instead of the Temporal runner entrypoint and still lacks the Temporal connection env vars used by the Podman Temporal stack. So Task 9 is still incomplete in the committed Kind assets even though the server manifest itself is now aligned with the repo’s Postgres credentials.

Recommended fix: update `deploy/kind/workflow-runner.yaml` to use the Temporal entrypoint and matching Temporal environment configuration, then verify the committed Kind deployment path from manifests alone.

### 5. Medium: custom approval policy is still not reachable from the public API surface
The previous caller-side contract gap also remains unchanged. `WorkflowInput` still supports `approval_policy`, but `RunWorkflowRequest` still has no typed policy field and the definition-backed API path still does not populate one. So custom auto-approval continues to exist only for internal/test-created `WorkflowInput` objects, not for real callers.

Recommended fix: add `approval_policy` to the real workflow-start contract and add API-level tests that prove it reaches `_handle_approval()`.

## Perspective Check
- Functionality: covered; the latest commit fixed one deployment mismatch but did not resolve the main workflow/runtime contract gaps.
- Quality: covered; focused verification passes, but it still does not prove the missing workflow-path and deployment-seam behavior.
- Security: covered; advisory/read-only enforcement is still not implemented at the actual execution boundary.

## Verification
- `git status --short`
- `git log -7 --stat --decorate`
- `git diff --name-only 9c19cfe8410e1383aa61c4c8c7bd399443d5a7f1..HEAD`
- `git diff 8d9480eddef28b1918c35119eea684014448aa6f..HEAD -- deploy/kind/temporal.yaml deploy/kind/temporal-lite.yaml`
- Read: `deploy/kind/temporal.yaml`, `deploy/kind/temporal-lite.yaml`, `deploy/kind/workflow-runner.yaml`, `src/agents/workflow/temporal_workflow.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/spawner/base.py`, `src/agents/spawner/podman_spawner.py`, `docs/design/cloud-agents/poc2/phase-2-implementation-review-round-3.md`
- `uv run pytest tests/integration/temporal/test_policy_integration.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/spawner/test_base.py -q` -> 49 passed

## Summary
This commit resolves the specific Kind Temporal/Postgres manifest mismatch from round 3, but it does not change the underlying Phase 2 implementation status. The phase is still not review-clean because the core workflow notification/config-ref path, spawner-boundary enforcement, Podman skills support, and Kind Temporal runner manifest are all still incomplete.
