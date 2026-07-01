# Review: Phase 1 implementation (`8fa1efd`)

## Findings

### 1. Medium: orphan cleanup metric overcounts successful cleanup on partial destroy failure
`reconcile_orphaned_sandboxes()` increments `ls_sandbox_orphans_cleaned_total` by `len(orphans)` whenever any orphaned sandboxes are found, even if one or more `spawner.destroy()` calls fail. That means dashboards will report containers as "cleaned" when they actually failed cleanup, which weakens the operational signal this phase was supposed to add. The current tests cover destroy failures being logged and swallowed, but they do not assert that the metric only counts successful destroys.

Recommended fix: track successful destroys separately and increment `ls_sandbox_orphans_cleaned_total` by the number of successful cleanup operations, not the number of discovered orphans. Add a regression test where one destroy fails and assert the metric increases only for the sandboxes that were actually removed.

## Perspective Check
- Functionality: `T1`, `T3`, and `T22` otherwise look correctly wired through the workflow path. `allowedTools` / `deniedTools` are forwarded into the sandbox request body, and `model_provider` flows from `ProviderConfig` into sandbox env construction with the intended fallback/override behavior.
- Quality: remaining gap is the cleanup metric accuracy and the missing regression test for partial orphan cleanup failure.
- Security: no major security regressions stood out in the reviewed phase scope. `T1` is still correctly scoped as forwarding-only rather than claiming enforcement, which matches the approved plan.

## Verification
- `git status --short --branch` -> `## main...origin/main`
- `git log -6 --stat --decorate --oneline` -> latest implementation commit is `8fa1efd`
- Reviewed implementation in:
  - `src/cloud_agents/workflow/temporal_activities.py`
  - `src/cloud_agents/workflow/temporal_entrypoint.py`
  - `src/cloud_agents/workflow/temporal_metrics.py`
  - `src/cloud_agents/workflow/temporal_models.py`
  - `src/cloud_agents/workflow/temporal_workflow.py`
- Reviewed matching tests in:
  - `tests/unit/workflow/temporal/test_activities.py`
  - `tests/unit/workflow/temporal/test_cleanup_metrics.py`
  - `tests/unit/workflow/temporal/test_models.py`
  - `tests/unit/workflow/temporal/test_entrypoint.py`
  - `tests/unit/workflow/temporal/test_startup_reconciliation.py`
- Ran:
  - `uv run pytest tests/unit/workflow/temporal/test_activities.py tests/unit/workflow/temporal/test_cleanup_metrics.py tests/unit/workflow/temporal/test_models.py tests/unit/workflow/temporal/test_entrypoint.py -q`
  - Result: `75 passed, 1 warning in 0.42s`

## Summary
Not `LGTM` yet. The phase implementation is close and the main runtime seams for `T1` and `T22` look sound, but `T3` currently overstates successful orphan cleanup in metrics when cleanup only partially succeeds.
