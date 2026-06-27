# Review: Phase 7 whole-scope follow-up (`57726e48..ba6e66de`)

## Findings

### 1. Medium: the updated Phase 7 task doc still contradicts itself about the shipped Kubernetes auth model
The latest commit tried to align `docs/design/cloud-agents/phase-7-tasks.md` with reality, but the doc still contains two incompatible descriptions of Task 3. In the implementation-specific subsection it now says Kubernetes uses the same shared `AGENT_API_TOKEN` model as Podman and explicitly defers TokenReview. But the later `Token model (deployment-specific)` subsection still says `Kubernetes (production)` uses projected ServiceAccount tokens and TokenReview validation. Those two descriptions cannot both be the shipped Phase 7 contract.

Why it matters:
- the whole-batch scope still has two competing sources of truth for a critical security boundary
- readers cannot tell whether Phase 7 actually shipped shared-secret K8s auth or per-pod identity with TokenReview
- this prevents a clean `LGTM` because the approved scope and the committed phase doc are still internally inconsistent

Recommended fix:
- update the `Token model (deployment-specific)` subsection so it matches the shipped implementation and the earlier Task 3 text, or explicitly move the per-pod TokenReview model into a future phase/backlog section

### 2. Medium: the task doc still appears to promise a dedicated integration test artifact that is not in the committed Phase 7 scope
On this whole-scope pass, `docs/design/cloud-agents/phase-7-tasks.md` still says Task 10 creates `tests/integration/agents/test_executor_spawner.py`, but there is still no such file in the tree. The focused verification now passes through unit tests plus the new Podman pytest E2E, so the actual verification story is stronger than before, but the phase task doc still does not describe the shipped test assets accurately.

Why it matters:
- the doc still overstates or misstates at least one concrete deliverable from the batch
- this leaves avoidable ambiguity about whether the integration test was intentionally descoped or accidentally omitted
- the whole-scope review cannot cleanly conclude `LGTM` while the committed task doc still diverges from the committed implementation artifacts

Recommended fix:
- either add the promised integration test artifact, or update Task 10 to describe the verification assets that Phase 7 actually ships

## Perspective Check
- Functionality: no major implementation issues found on this pass; the focused Phase 7 suite now passes in this environment.
- Quality: remaining gaps. The phase task doc still has internal drift and does not yet cleanly match the shipped batch.
- Security: remaining gaps. The shipped Kubernetes auth model is documented inconsistently inside the task doc.

## Verification
- Performed another whole-scope re-review over the Phase 7 range:
  - `57726e48..ba6e66de`
- Re-read the current phase scope and prior review:
  - `docs/design/cloud-agents/phase-7-tasks.md`
  - `docs/design/cloud-agents/phase-7-implementation-review-5.md`
- Re-checked the current implementation and verification config:
  - `src/agents/runtime/auth.py`
  - `src/agents/workflow/executor.py`
  - `src/agents/workflow/advancement.py`
  - `tests/e2e/test_phase7_podman_pytest.py`
  - `pyproject.toml`
- Searched the updated task doc for the Kubernetes auth contract and found conflicting statements:
  - shared-secret K8s auth in Task 3 implementation text
  - projected SA token + TokenReview still present in the later token-model section
- Searched for the promised Task 10 integration test artifact:
  - `tests/integration/agents/test_executor_spawner.py`
  - Result: not present
- Ran focused Phase 7 verification:
  - `uv run pytest tests/unit/agents/test_phase7_security.py tests/unit/agents/test_phase7_robustness.py tests/unit/agents/workflow/test_auto_approve.py tests/unit/agents/workflow/test_definition_api.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/spawner/test_kubernetes_spawner.py tests/unit/agents/spawner/test_podman_spawner.py tests/e2e/test_phase7_podman_pytest.py -q`
  - Result: `75 passed`

## Summary
Not `LGTM` yet. The latest follow-up resolves the flaky pytest issue and the focused Phase 7 suite now passes here, but the required whole-scope pass still finds doc-to-batch drift: the Phase 7 task doc still contradicts itself about the Kubernetes auth model and still appears to promise an integration test artifact that is not committed. Once the committed phase doc matches the committed implementation and verification assets, this looks close.
