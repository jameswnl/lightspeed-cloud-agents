# Phase 2 Implementation Review

**Reviewer**: Claude Opus (lightspeed-agentic-operator session)
**Date**: 2026-07-01
**Scope**: All Phase 2 code changes (commits `38dd127..440fb12`), compared against `implementation-plan.md` and `t7-rbac-design.md`.

## Verdict: Two blockers, otherwise solid (~90% complete)

## Blockers

### 1. ApproverInfo not persisted in workflow state

`temporal_api.py` captures `ApproverInfo` in the approve endpoint and emits an audit event, but:
- The `ApproverInfo` is NOT passed to the `AgentWorkflow.approve()` signal
- `temporal_workflow.py`'s approve method signature doesn't accept an `ApproverInfo` parameter
- Approver identity exists only in audit logs, not in workflow state

This fails verification checklist item: "Approver identity recorded in workflow state." The design (t7-rbac-design.md, Step 7) explicitly requires this.

**Fix**:
1. Add `approver: Optional[ApproverInfo] = None` to `AgentWorkflow.approve()` signal
2. Store `approver` in the step result alongside the decision
3. Pass `ApproverInfo` from the API handler's signal call
4. Expose via `get_status()` query

### 2. WorkflowRunnerNotReady alert expression differs from plan

Plan specifies:
```
expr: probe_success{job="workflow-runner", path="/readyz"} == 0
```

Implementation uses:
```
expr: up{job="workflow-runner"} == 1 unless on() (ls_workflow_runs_total)
```

These detect different failure modes. The plan's intent was Temporal connectivity loss (readyz probe). The implementation checks for "up but no workflows processed" — a different and weaker signal.

**Fix**: Either restore the plan's expression, or update the plan with rationale for the change.

## Medium

### 3. No Helm template tests (T17, T24)

The plan specifies shell-based Helm template tests:
- `test_helm_template_renders_prometheusrule` / `test_helm_template_no_prometheusrule_by_default`
- `test_pdb_rendered_when_replicas_gt_1` / `test_pdb_not_rendered_with_single_replica` / `test_pdb_not_rendered_when_disabled`

No test files exist for these.

### 4. No CircuitBreakerOpen alert rule

The plan (T17 line 90) states the LLM provider error alert is a joint T17+T19 deliverable. T19 adds circuit breaker metrics but T17's PrometheusRule has no corresponding `CircuitBreakerOpen` alert.

## Low

### 5. Import style inconsistency in temporal_api.py

Multiple imports inside function bodies at varying locations. Functional but inconsistent.

### 6. Policy file load has no error context

`PolicyFileAuthorizer.__init__` doesn't wrap file/YAML errors with context about which file failed.

## Task Completion Status

| Task | Status | Notes |
|---|---|---|
| T7 (RBAC) | 85% | ApproverInfo persistence missing (blocker #1) |
| T17 (Alerting) | 80% | Alert expression differs (blocker #2), no CircuitBreakerOpen alert, no tests |
| T19 (Circuit breaker) | 100% | Fully implemented and tested |
| T21 (Interpolation) | 100% | Fully implemented, 29 tests |
| T24 (PDB) | 95% | Template correct, no tests |

## Verification Checklist

| Item | Pass? |
|---|---|
| T17: PrometheusRule renders | Partial — no tests, alert expression differs |
| T19: Circuit breaker opens/resets | Pass |
| T19: Open breaker prevents spawning | Pass |
| T21: Recursive `{{` not expanded | Pass |
| T21: Large values truncated | Pass |
| T24: PDB rendered when replicas > 1 | Not tested |
| T7: Unauthorized trigger → 403 | Pass |
| T7: Authorized trigger succeeds | Pass |
| T7: Approver identity in workflow state | **FAIL** (blocker #1) |
| T7: Policy file rules enforced | Pass |
| T7: Fail-closed: authz + no identity → 401 | Pass |
| T7: Unauthorized GET /definitions → 403 | Pass |
| T7: Unauthorized POST /definitions → 403 | Pass |
| All existing tests still pass (322 baseline) | Not verified |
