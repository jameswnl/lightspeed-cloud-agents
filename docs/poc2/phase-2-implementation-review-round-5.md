# Review: Phase 2 implementation round 5 (`1e2ec15..d7e8f71`)

## Findings

### 1. Major: notifier/escalation config refs are still only threaded through models, not resolved or used at runtime
This commit wires `send_approval_notification()` into `_handle_approval()`, adds `notifier_config` / `escalation_config` fields to `WorkflowInput`, and passes `workflow_name` into `build_escalation_activity()`. But the actual worker-side delivery contract is still not implemented. `send_approval_notification()` still hardcodes `NullNotifier()` and ignores `notifier_config`, and `build_escalation_activity()` still hardcodes `LogPackager()` and ignores `escalation_config`. So the system still cannot perform the secret-safe runtime resolution promised by the Phase 2 contract, and the new fields are effectively unused.

Recommended fix: resolve `notifier_config` and `escalation_config` inside the worker activity into concrete notifier/packager implementations, then add tests that prove the selected implementation is used and that raw secrets do not pass through Temporal payloads.

### 2. Major: Podman advisory enforcement is still not implemented at the execution boundary
The K8s service-account path is now wired through to the spawner, and advisory mode defaults to `advisory-sa` when no explicit SA is given. But the Podman half of the advisory contract still is not implemented. `run_sandbox_step()` does not pass any read-only execution flag, `PodmanSpawner._do_spawn()` still ignores advisory entirely, and the configured host mounts in `_volume_mounts` are still mounted normally. That means advisory mode is still enforced only by prompt/output shaping on the Podman path, not by the runtime boundary required by the Phase 2 plan.

Recommended fix: add an explicit read-only/advisory execution mode to the spawner contract and make `PodmanSpawner` use it to run the agent container read-only and omit host mounts in advisory mode, then add Podman-specific tests that assert that boundary behavior.

## Perspective Check
- Functionality: covered; several prior findings are fixed, but the remaining delivery-config and Podman-advisory seams are still real behavioral gaps.
- Quality: covered; focused tests pass, but they still do not prove the runtime config-ref path or Podman advisory boundary behavior.
- Security: covered; K8s trust-boundary handling improved, but Podman advisory execution is still not enforced at the runtime boundary.

## Verification
- `git status --short`
- `git log -1 --stat --decorate`
- `git diff --name-only 1e2ec150db56d76b89394f4fa92364931786a281..HEAD`
- `git diff 1e2ec150db56d76b89394f4fa92364931786a281..HEAD -- deploy/kind/workflow-runner.yaml src/agents/spawner/base.py src/agents/spawner/kubernetes_spawner.py src/agents/spawner/podman_spawner.py src/agents/workflow/temporal_activities.py src/agents/workflow/temporal_api.py src/agents/workflow/temporal_models.py src/agents/workflow/temporal_workflow.py tests/unit/agents/runtime/test_token_review.py`
- Read: `deploy/kind/workflow-runner.yaml`, `src/agents/spawner/base.py`, `src/agents/spawner/kubernetes_spawner.py`, `src/agents/spawner/podman_spawner.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/workflow/temporal_api.py`, `src/agents/workflow/temporal_models.py`, `src/agents/workflow/temporal_workflow.py`, `tests/unit/agents/spawner/test_skills_image.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/integration/temporal/test_policy_integration.py`, `tests/unit/agents/runtime/test_token_review.py`
- `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/integration/temporal/test_policy_integration.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/spawner/test_base.py -q` -> 61 passed

## Summary
This follow-up commit resolves several of the earlier review findings: `approval_policy` is now exposed on the API, K8s service-account override is wired to the spawner, Podman skills-image extraction is implemented, and the Kind workflow-runner manifest now uses the Temporal entrypoint. The remaining blockers are narrower but still material: config-ref delivery is still stubbed behind `NullNotifier` / `LogPackager`, and Podman advisory mode still lacks the required runtime-boundary enforcement.
