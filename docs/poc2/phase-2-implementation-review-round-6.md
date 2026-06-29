# Review: Phase 2 implementation round 6 (`d7e8f71..fd555e0`)

## Findings

### 1. Major: notifier/escalation config refs are still not reachable from the real workflow start surface
This commit resolves config refs inside the activities themselves, but the values still are not plumbed from the real workflow entrypoints. `WorkflowInput` carries `notifier_config` and `escalation_config`, and the workflow passes them into the activities, but `RunWorkflowRequest` still does not expose either field and there are no caller-side tests for them. Right now the new runtime resolution logic is only reachable for code that constructs `WorkflowInput` directly, which means the public workflow-start surface still cannot exercise the secret-safe delivery contract.

Recommended fix: add typed `notifier_config` and `escalation_config` fields to the real workflow start contract and add API-level tests that prove they reach the activities.

### 2. Medium: Podman advisory mode is read-only now, but still does not omit host mounts as required by the Phase 2 contract
The new `read_only` flag is a real improvement and does enforce a read-only filesystem in `PodmanSpawner`. But the Phase 2 advisory contract also required "no host mounts" on the Podman path. `PodmanSpawner._do_spawn()` still mounts `self._volume_mounts` unconditionally before applying `read_only`, so advisory sessions still receive those host-mounted paths. That leaves the Podman trust-boundary implementation narrower than the agreed contract.

Recommended fix: when `read_only=True`, skip the normal host volume mounts and keep only the minimum mounts required for the advisory execution contract, then add a Podman-focused test that asserts those mounts are omitted.

## Perspective Check
- Functionality: covered; most of the previous runtime seams are fixed, but delivery config is still not caller-reachable and Podman advisory enforcement is still contract-incomplete.
- Quality: covered; focused tests pass, but there is still no caller-side proof for the new config-ref path and no test for advisory-mode host-mount omission on Podman.
- Security: covered; Podman boundary enforcement improved materially, but it still does not fully match the no-host-mount advisory contract.

## Verification
- `git status --short`
- `git log -1 --stat --decorate`
- `git diff --name-only d7e8f7161daf21261a284139f1e2e544001a45f7..HEAD`
- `git diff d7e8f7161daf21261a284139f1e2e544001a45f7..HEAD -- src/agents/spawner/base.py src/agents/spawner/kubernetes_spawner.py src/agents/spawner/podman_spawner.py src/agents/workflow/temporal_activities.py`
- Read: `src/agents/spawner/base.py`, `src/agents/spawner/kubernetes_spawner.py`, `src/agents/spawner/podman_spawner.py`, `src/agents/workflow/temporal_activities.py`, `src/agents/workflow/temporal_api.py`, `src/agents/workflow/temporal_models.py`, `src/agents/workflow/temporal_workflow.py`, `tests/unit/agents/spawner/test_skills_image.py`, `tests/unit/agents/workflow/temporal/test_api.py`, `tests/integration/temporal/test_policy_integration.py`, `tests/unit/agents/runtime/test_token_review.py`
- `uv run pytest tests/unit/agents/runtime/test_token_review.py tests/integration/temporal/test_policy_integration.py tests/unit/agents/workflow/temporal/test_api.py tests/unit/agents/workflow/temporal/test_activities.py tests/unit/agents/workflow/temporal/test_workflow.py tests/unit/agents/spawner/test_skills_image.py tests/unit/agents/spawner/test_base.py -q` -> 61 passed

## Summary
This follow-up resolves the two core round-5 issues substantially: config refs are now interpreted inside the activities, and Podman advisory mode now uses a read-only filesystem. The phase is still not review-clean, though, because those delivery-config refs are not yet reachable from the public workflow start surface, and the Podman advisory implementation still does not satisfy the no-host-mount part of the agreed contract.
