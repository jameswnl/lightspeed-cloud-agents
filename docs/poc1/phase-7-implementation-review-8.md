# Review: Phase 7 whole-scope follow-up (`57726e48..288338d6`)

## Findings

### 1. Medium: the task doc still contains one internal inconsistency for deferred Kubernetes spawner details
The latest doc update correctly marks Kubernetes `AlreadyExists` handling and workflow visibility labels as deferred to backlog in the Task 4/5 fix bullets, but `docs/design/cloud-agents/phase-7-tasks.md` still has a stale `Files:` entry under Task 5: `Modify: src/agents/spawner/kubernetes_spawner.py — add workflow labels to Jobs`. That file-level instruction still reads like shipped Phase 7 work even though the corresponding behavior is now explicitly marked deferred above it.

Why it matters:
- the whole-batch phase doc still has one remaining mixed signal about what shipped versus what was deferred
- this is small, but it is still a scope/traceability inconsistency in the final pass
- I do not want to write `LGTM` while the committed phase doc still disagrees with itself, even in a narrow way

Recommended fix:
- update the Task 5 `Files:` list so it matches the deferred status, or annotate that entry as backlog/deferred instead of shipped Phase 7 work

## Perspective Check
- Functionality: no major implementation/runtime issues found on this pass.
- Quality: remaining gaps. One narrow task-doc inconsistency still remains in the final whole-batch scope.
- Security: no major new issues found on this pass beyond the documented shipped/shared-secret model.

## Verification
- Performed another whole-scope re-review over the full Phase 7 batch:
  - `57726e48..288338d6`
- Re-read the current phase scope and prior review:
  - `docs/design/cloud-agents/phase-7-tasks.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-7.md`
- Re-checked the updated Task 4/5 text against the remaining file-level instructions in `docs/design/cloud-agents/phase-7-tasks.md`
- Reused the latest focused verification result since this follow-up commit only changed review/docs:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py tests/e2e/test_phase7_podman_pytest.py -q`
  - Result: `75 passed`

## Summary
Not `LGTM` yet. This is very close, but the required whole-batch pass still finds one narrow inconsistency in the committed Phase 7 task doc: the Task 5 `Files:` entry still reads like shipped work even though the corresponding Kubernetes workflow labels are now marked deferred. Once that is aligned, I expect the batch to be ready for a final `LGTM` pass.
