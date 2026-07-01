# Review: Phase 2 implementation (`4ab5923..ae99d43`)

## Findings

### 1. Major: later-operation authz silently degrades if the workflow authz-context query fails
The new `_get_workflow_authz()` helper is the right seam for `approve` / `view` / `cancel`, but it wraps the Temporal query in a broad `except Exception: pass` and falls back to `WorkflowResource(workflow_id=workflow_id)`. That means if the query fails transiently, later authorization checks lose the stored `owner`, `workflow_name`, and `namespace` fields they rely on. Under owner-scoped or workflow-scoped policies, that can turn a deny into an allow because the authorizer only sees partial resource context. The test run already hints at this path: three API tests now emit runtime warnings from the swallowed-exception branch in `temporal_api.py`.

Recommended fix: fail closed when authz-context lookup fails for actions that depend on stored workflow metadata, rather than authorizing against a stripped-down resource. Add a regression test where the query fails and verify the endpoint returns an authorization/lookup error instead of continuing with incomplete context.

### 2. Major: `WorkflowRunnerNotReady` does not actually detect Temporal connectivity loss
The new alert expression is `up{job="workflow-runner"} == 1 unless on() (ls_workflow_runs_total)`. That is not a readiness or Temporal-connectivity signal. On a fresh or idle deployment that has not processed any workflows yet, this alert will fire after 10 minutes even if the runner is perfectly healthy. Conversely, once any workflow has ever run and `ls_workflow_runs_total` exists, the alert may stop firing even if the runner later loses Temporal connectivity. This is a real runtime-behavior mismatch between the alert name/summary and the metric it evaluates.

Recommended fix: alert on an actual readiness/Temporal-health signal, or add instrumentation that explicitly exposes Temporal connectivity status. If no such signal exists yet, this alert should be deferred rather than approximated with workflow-run traffic.

### 3. Medium: `CircuitBreakerOpen` is still inferred from the wrong metric source
The new `CircuitBreakerOpen` alert is driven by `increase(ls_sandbox_cleanup_failures_total[5m]) > 3`, but cleanup failures are not the same thing as the provider circuit breaker opening. A provider outage can open the breaker without any destroy failures, while container cleanup failures can spike for unrelated reasons even when the breaker never opens. There is still no dedicated breaker metric in the implementation, so the alert does not actually measure the behavior it claims to report.

Recommended fix: expose an explicit circuit-breaker metric (for example open/closed state or open events keyed by provider) and base the alert on that metric. Until then, either rename this alert to describe what it really measures or defer it.

## Perspective Check
- Functionality: the big T7 identity/context/state gaps from round 1 are fixed, but the monitoring path still has two behavior mismatches and authz still degrades incorrectly on context-query failure.
- Quality: tests are now broader, but they still permit a swallowed-exception path in authz lookup, and the alerting changes are not yet grounded in the actual runtime signals they name.
- Security: the middleware seam is fixed, but failing open-ish to a partial `WorkflowResource` on authz-context lookup failure is still too permissive for owner/workflow-scoped authorization.

## Verification
- Scope selected: **specific phase implementation** across Phase 2 code commits `4ab5923..ae99d43`
- `git status --short --branch` -> `## main...origin/main`
- `git log -4 --stat --decorate --oneline` -> reviewed follow-up commits:
  - `d080260` (`Phase 2 implementation review: fix 5 findings`)
  - `ae99d43` (`Phase 2 review round 2: add CircuitBreakerOpen alert, note blockers already fixed`)
- Reviewed implementation files:
  - `src/cloud_agents/runtime/auth.py`
  - `src/cloud_agents/workflow/temporal_api.py`
  - `src/cloud_agents/workflow/temporal_workflow.py`
  - `src/cloud_agents/workflow/temporal_activities.py`
  - `deploy/helm/templates/prometheusrule.yaml`
- Reviewed matching tests:
  - `tests/unit/workflow/temporal/test_api.py`
  - `tests/unit/workflow/temporal/test_activities.py`
  - `tests/unit/workflow/test_authorization.py`
  - `tests/unit/workflow/test_policy_authorizer.py`
  - `tests/unit/workflow/test_circuit_breaker.py`
  - `tests/unit/workflow/test_interpolation.py`
- Ran:
  - `uv run pytest tests/unit/workflow/temporal/test_api.py tests/unit/workflow/test_authorization.py tests/unit/workflow/test_policy_authorizer.py tests/unit/workflow/test_circuit_breaker.py tests/unit/workflow/test_interpolation.py tests/unit/workflow/temporal/test_activities.py -q`
  - Result: `159 passed, 4 warnings in 0.74s`
  - Notable warning: three tests emitted `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` from `src/cloud_agents/workflow/temporal_api.py`, consistent with the swallowed exception path in `_get_workflow_authz()`

## Summary
Not `LGTM` yet. Phase 2 is much closer after the latest fixes, especially on the T7 runtime path, but there are still meaningful seams in the authz-context fallback and in the new alerting rules that prevent a clean approval.
