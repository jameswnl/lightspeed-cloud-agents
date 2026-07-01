# Review: Phase 2 implementation (`4ab5923..440fb12`)

## Findings

### 1. Blocker: T7 authorization is not wired to real caller identity at runtime
The new authorization layer depends on `get_caller_identity()`, but neither `BearerAuthMiddleware` nor `TokenReviewAuthMiddleware` ever sets `request.state.caller_identity`. In the real app this means Phase 2 authz either fails closed for every request when `WORKFLOW_AUTHZ != none`, or only works in tests that manually inject `request.state.caller_identity`. The focused suites pass because they exercise the dependency and API handlers in isolation, not the middleware-to-handler seam.

Recommended fix: make the auth middleware populate a real `CallerIdentity` on successful authentication for each supported auth mode, then add an integration-style test that exercises the actual middleware path through the FastAPI app and proves authorized requests succeed while unauthorized ones fail.

### 2. High: T7 later-operation authorization does not use the persisted authz context it was designed around
`run_workflow()` captures `WorkflowAuthzContext`, but `approve`, `get`, `events`, and `cancel` authorize only against `WorkflowResource(workflow_id=..., step=...)` without first loading the persisted authz context. As a result, owner-scoped or workflow-name-scoped rules cannot be evaluated for those later operations, because the `PolicyFileAuthorizer` only sees empty `owner` / `workflow_name` / `namespace` fields. This is a classic “field exists in the model but runtime never uses it” seam failure.

Recommended fix: before authorizing `approve`, `view`, `events`, and `cancel`, query the workflow for its persisted authz context and populate `WorkflowResource` with the stored owner/workflow/namespace fields. Add tests that prove an owner-scoped rule actually denies a non-owner on an existing workflow.

### 3. High: approver identity is audited, but not actually recorded in workflow state as the Phase 2 design claims
The approval endpoint builds an `ApproverInfo`, but only writes it into the audit event. The workflow signal still accepts just `(step_name, decision, selected_option_id)`, `AgentWorkflow` stores only those values in `_approval_decisions`, and the resulting `StepResult` contains no approver fields. That means the implementation does not satisfy the stated contract that approver identity is queryable from workflow state.

Recommended fix: extend the approval signal and workflow state to carry `ApproverInfo`, include it in the resulting step output/status model, and add an assertion on the queryable workflow status rather than only on emitted audit details.

### 4. Medium: the new `WorkflowRunnerNotReady` alert depends on a metric source the deployment does not create
The PrometheusRule uses `probe_success{job="workflow-runner", path="/readyz"}`, but there is no matching `ServiceMonitor`, `PodMonitor`, blackbox probe config, or other Helm asset in this phase that would create that series. The only reference to `probe_success` in the repo is the alert rule itself. So the alert looks valid in template tests but may never evaluate meaningfully in a real deployment.

Recommended fix: either add the monitoring asset that produces the `probe_success` series, or rewrite the alert to use metrics/signals the deployment actually exposes today. Add a deployment-level verification step that proves the referenced series exists in the intended monitoring stack.

### 5. Medium: T19 breaker accounting misses important failure paths
The circuit breaker records failures only when the sandbox returns `success=false`. It does not record failures for several important error paths that are likely to dominate during provider outages or infra incidents: readiness failure, HTTP 502 raising `RuntimeError`, spawner errors, and other exceptions before the JSON success check. In those cases the activity can fail repeatedly without ever tripping the breaker, which undercuts the fail-fast behavior this task is supposed to add.

Recommended fix: record provider failures for exception paths that represent provider/step execution failure, not just `success=false` responses. Add regression tests for at least one raised-exception path (for example HTTP 502 or readiness failure) and assert the breaker state advances.

## Perspective Check
- Functionality: major gaps remain in the T7 runtime contract and in T19 exception-path breaker behavior.
- Quality: tests are strong at the helper/unit level, but they currently miss key seams between middleware, API handlers, workflow state, and deployed monitoring assets.
- Security: the intended access-control model is not actually operational yet because caller identity never reaches request state and later authorization checks do not load the stored authz context.

## Verification
- Scope selected: **specific phase implementation** across Phase 2 code commits `4ab5923..440fb12`
- `git status --short --branch` -> `## main...origin/main`
- `git log -6 --stat --decorate --oneline` -> Phase 2 implementation landed in:
  - `4ab5923` (`T17`, `T19`, `T21`, `T24`)
  - `440fb12` (`T7`)
- `git diff --name-only 3fe4b07..HEAD` used to identify changed files in scope
- Reviewed implementation files:
  - `src/cloud_agents/runtime/auth.py`
  - `src/cloud_agents/workflow/authorization.py`
  - `src/cloud_agents/workflow/policy_authorizer.py`
  - `src/cloud_agents/workflow/circuit_breaker.py`
  - `src/cloud_agents/workflow/temporal_api.py`
  - `src/cloud_agents/workflow/temporal_activities.py`
  - `src/cloud_agents/workflow/temporal_workflow.py`
  - `src/cloud_agents/workflow/interpolation.py`
  - `src/cloud_agents/workflow/temporal_models.py`
  - `deploy/helm/templates/prometheusrule.yaml`
  - `deploy/helm/templates/pdb.yaml`
  - `deploy/helm/values.yaml`
- Reviewed matching tests:
  - `tests/unit/workflow/temporal/test_api.py`
  - `tests/unit/workflow/test_authorization.py`
  - `tests/unit/workflow/test_policy_authorizer.py`
  - `tests/unit/workflow/test_circuit_breaker.py`
  - `tests/unit/workflow/test_interpolation.py`
  - `tests/unit/workflow/temporal/test_activities.py`
- Ran:
  - `uv run pytest tests/unit/workflow/temporal/test_api.py tests/unit/workflow/test_authorization.py tests/unit/workflow/test_policy_authorizer.py tests/unit/workflow/test_circuit_breaker.py tests/unit/workflow/test_interpolation.py tests/unit/workflow/temporal/test_activities.py -q`
  - Result: `150 passed, 1 warning in 0.82s`

## Summary
Not `LGTM`. The helper-level Phase 2 tests are passing, but there are still meaningful seam failures between the designed behavior and the actual runtime: T7 authorization is not yet wired through the real auth path or persisted workflow context, approver identity is not stored in workflow state, and both T17 and T19 have deploy/runtime gaps that the current tests do not catch.
