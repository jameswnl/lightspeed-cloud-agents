# Review: Phase 7 whole-scope follow-up (`57726e48..f3603081`)

## Findings

### 1. Medium: the Phase 7 task doc still promises Kubernetes spawner behaviors that are not implemented
The latest doc fix removed the contradictory auth text and stale test references, but the whole-batch pass still finds one remaining doc-to-implementation mismatch. `docs/design/cloud-agents/phase-7-tasks.md` still says Task 4 includes `KubernetesSpawner: handle AlreadyExists on Job creation` and Task 5 adds `spawner_labels` (`workflow-id`, `step-name`, `created-at`) to spawned Jobs. The current `src/agents/spawner/kubernetes_spawner.py` still does neither: it creates Jobs directly without `AlreadyExists` handling and only sets `{"app": agent_name, "spawned-by": "workflow-runner"}` labels.

Why it matters:
- the committed phase scope still overstates at least part of the shipped Kubernetes robustness behavior
- this prevents a clean whole-batch `LGTM`, because the task doc still does not fully match the implementation it is claiming is complete
- these are the exact kinds of idempotency/visibility details that matter for operational debugging and crash recovery

Recommended fix:
- either implement the promised `AlreadyExists` handling and workflow visibility labels in `src/agents/spawner/kubernetes_spawner.py`, or
- update `docs/design/cloud-agents/phase-7-tasks.md` to narrow Tasks 4-5 to the behavior that actually shipped

## Perspective Check
- Functionality: no major implementation/runtime issues found on this pass; the focused Phase 7 suite passes in this environment.
- Quality: remaining gaps. The whole-batch phase doc still does not fully match the shipped Kubernetes spawner behavior.
- Security: no major new issues found on this pass beyond the documented shipped/shared-secret model.

## Verification
- Performed another whole-scope re-review over the full Phase 7 batch:
  - `57726e48..f3603081`
- Re-read the current phase scope and prior review:
  - `docs/design/cloud-agents/phase-7-tasks.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-6.md`
- Re-checked the shipped Kubernetes spawner implementation:
  - `src/agents/spawner/kubernetes_spawner.py`
- Searched the current phase doc for remaining promised K8s spawner details:
  - found `AlreadyExists` handling still listed under Task 4
  - found workflow visibility labels (`workflow-id`, `step-name`, `created-at`) still listed under Task 5
- Verified the focused Phase 7 suite still passes:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py tests/e2e/test_phase7_podman_pytest.py -q`
  - Result: `75 passed`

## Summary
Not `LGTM` yet. The latest follow-up resolved the prior doc inconsistencies and the focused Phase 7 suite passes, but the required whole-batch pass still finds one remaining doc-to-implementation mismatch in the Kubernetes spawner details. Once the task doc is fully aligned with the shipped K8s robustness behavior, this should be very close.
