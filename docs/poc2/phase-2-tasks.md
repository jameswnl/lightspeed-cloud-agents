# PoC2 Phase 2: Production Readiness

## Context

Phase 1 delivered the core Temporal engine and received LGTM after 4 review rounds. Phase 2 wires the five existing policy modules into the Temporal code path, adds deployment infrastructure, and proves it works E2E. This is the final phase.

## Tasks

### Task 1: Wire auto-approval into `_handle_approval()`

Import `classify_step_risk`, `ApprovalPolicy` via `workflow.unsafe.imports_passed_through()`. Add `approval_policy` to `WorkflowInput`. In `_handle_approval()`: reconstruct `WorkflowStepSpec`, classify risk, auto-approve if safe. Emit `step.auto_approved` event.

Tests: low-risk auto-approves, high-risk waits, no risk_level defaults to manual, custom policy.

### Task 2: Wire advisory mode into workflow + activity

Add `advisory: bool = False` to `WorkflowInput` and `WorkflowDefinition`. In workflow: `annotate_prompt()` before dispatch, `should_skip_approval()` for approval steps, `annotate_output()` after completion. In activity: pass `advisory` flag in sandbox request.

Tests: prompt annotation, approval skip, output marking, non-advisory unchanged.

### Task 3: Wire permissions into sandbox activity

Extract `permissions` from step dict in `run_sandbox_step`. Pass `service_account` to spawner, `allowed_tools`/`denied_tools` in sandbox request. Use `timeout_seconds` override if set.

Tests: service_account forwarded, tool filters in request, timeout override, defaults when None.

### Task 4: Wire notification activity for approval pauses

New `send_approval_notification` activity using `notifier.py`. Workflow dispatches fire-and-forget on pause. Add `notifier_config` to `WorkflowInput`. Register in worker.

Tests: Slack notifier called, webhook called, NullNotifier default, workflow dispatches on pause.

### Task 5: Wire escalation delivery into activity

Extend `build_escalation_activity` to use `EscalationPackager` from `escalation.py`. Add `escalation_config` to `WorkflowInput`. Packager failure non-fatal.

Tests: LogPackager default, WebhookPackager called, delivery failure non-fatal, metadata flows.

### Task 6: Advisory flag on WorkflowDefinition + API propagation

`RunWorkflowRequest.advisory` overrides definition-level setting. Definition store resolution propagates advisory.

Tests: API propagates from request, from definition, request overrides definition.

**Depends on:** Task 2.

### Task 7: Skills image support in spawners

Add `skills_image`/`skills_paths` to `AgentSpawner.spawn()`. K8s: init container. Podman: `podman cp`. Activity forwards params.

Tests: K8s init container spec, Podman volume, activity forwards params.

### Task 8: Temporal Server deployment — Podman

`deploy/podman/docker-compose.temporal.yaml`: temporal-server, temporal-ui, temporal-db, workflow-runner.

### Task 9: Temporal Server deployment — Kind

`deploy/kind/temporal.yaml`: Temporal Server + Service + PostgreSQL. Update workflow-runner manifest.

### Task 10: E2E test — Podman

Multi-step workflow: diagnose→approve→fix, auto-approval for low-risk, advisory mode skip.

**Depends on:** Tasks 1, 2, 8.

### Task 11: E2E test — Kind

Full lifecycle: spawn→HTTP→destroy with real pods, escalation on failure, parallel groups.

**Depends on:** Tasks 1-7, 9.

### Task 12: Integration tests with Temporal test environment

Policy combinations using `WorkflowEnvironment.start_time_skipping()`: mixed risk levels, advisory E2E, permissions in request, notification on pause, escalation delivery.

**Depends on:** Tasks 1-5.

### Task 13: Independent review

Format/verify, full test suite, independent reviewer subagent. Address findings until LGTM.

**Depends on:** All tasks.

## Dependencies

```
Tasks 1-5, 7, 8, 9  (parallel)
     |
Task 6 (depends on Task 2)
     |
Task 12 (depends on Tasks 1-5)
Task 10 (depends on Tasks 1, 2, 8)
Task 11 (depends on Tasks 1-7, 9)
     |
Task 13 (depends on all)
```

## Execution Order

1. Tasks 1, 2, 3, 4, 5 (policy wiring — parallel)
2. Tasks 6, 7, 8, 9 (definition + infra — parallel, Task 6 after Task 2)
3. Task 12 (integration tests — after policy wiring)
4. Tasks 10, 11 (E2E tests — after infra)
5. Task 13 (review)

## Verification

- `uv run pytest tests/unit/agents/ -q` — all pass
- `uv run make format && uv run make verify` — clean
- E2E: Podman compose + Kind cluster both run workflows
- Independent reviewer LGTM
