# Review: Phase 8 full-scope re-review

## Findings

### 1. Medium: `phase-8-tasks.md` is still internally inconsistent about the K8s callback trust boundary
The later Task 10 section now correctly says the implemented scope is audience-scoped TokenReview and explicitly defers per-job identity binding to backlog. But the earlier `K8s Trust Boundary` section still says:

- Kubernetes callback mode is not production-ready until Task 10 is completed
- Task 10 closes the gap by making each spawned Job's projected SA token the only valid callback credential

Those statements no longer match the rescaled Task 10 contract that the latest commits established. The current shipped behavior is “audience-scoped TokenReview now, per-job binding deferred,” not “specific spawned job token only” in Phase 8.

This matters because `phase-8-tasks.md` is the behavioral contract for the phase. A reader doing a fresh review today would still get two different answers from the same file about what Kubernetes async-callback security guarantees Phase 8 actually delivers.

Recommended fix: update the earlier `K8s Trust Boundary` section so it matches the implemented Task 10 scope and the backlog entry.

### 2. Medium: Task 9 still over-claims compared with the committed E2E coverage
`tests/e2e/test_phase8_multi_replica.py` now honestly describes itself as infrastructure-only, but `phase-8-tasks.md` still presents Task 9 as the “integration capstone” covering happy path workflow completion, duplicate/stale callbacks, lost-callback poller recovery, crash-after-persist recovery, orphan reconciliation, and label visibility.

The committed E2E script does not submit a workflow or exercise those runtime behaviors; it checks cluster bring-up, healthz, basic ingest endpoint responses, and label selection. That means the phase-wide task doc still claims stronger integration verification than the repository actually contains.

Recommended fix: either narrow Task 9 in `phase-8-tasks.md` to match the shipped infra-smoke coverage, or add the missing runtime scenarios before calling the phase fully verified.

## Perspective Check
- Functionality: no new code-level blocker found in this final pass; the main remaining gaps are contract/verification mismatches.
- Quality: remaining gaps. The phase task doc still overstates both the K8s trust-boundary guarantee and the E2E verification scope.
- Security: implementation now matches the rescaled audience-scoped TokenReview model, but the phase doc still partially describes the stronger per-job-bound model as if it were completed.

## Verification
- Inspected full phase scope from `c66952ad..HEAD`
- Re-read:
  - `docs/design/cloud-agents/phase-8-tasks.md`
  - `docs/design/cloud-agents/BACKLOG.md`
  - `tests/e2e/test_phase8_multi_replica.py`
- Re-ran the focused phase-8 unit suite:
  - `uv run pytest tests/unit/agents/workflow/test_ingest.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_advance.py tests/unit/agents/workflow/test_step_dispatcher.py tests/unit/agents/workflow/test_executor_dual_mode.py tests/unit/agents/workflow/test_api.py tests/unit/agents/runtime/test_callback.py tests/unit/agents/runtime/test_token_review.py tests/unit/agents/test_remote_agent_client.py tests/unit/agents/spawner/test_kubernetes_spawner.py -q`
  - Result: `93 passed`

## Summary
The implementation itself looks substantially improved and the earlier code-level blockers appear addressed. I am still not writing `LGTM` because the phase contract is not fully self-consistent yet: `phase-8-tasks.md` still overstates the delivered K8s trust-boundary guarantee and the shipped E2E coverage.
