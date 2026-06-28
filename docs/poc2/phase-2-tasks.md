# PoC2 Phase 2: Production Readiness

## Context

Phase 1 delivered the core Temporal engine and received LGTM after 4 review rounds. Phase 2 wires the five existing policy modules into the Temporal code path, adds deployment infrastructure, and proves it works E2E. This is the final phase.

## Contracts (from plan review round 1)

### Advisory Enforcement Contract

Advisory mode is operationally read-only, not merely a UX label. Enforcement path:
- **K8s**: Advisory steps spawn with a read-only `service_account` (e.g., `advisory-sa`) that has only `get`/`list`/`watch` RBAC verbs. The workflow sets `service_account: "advisory-sa"` on the spawner call when `advisory=true`.
- **Podman**: Advisory steps spawn with `--read-only` filesystem and no host mounts. The Podman spawner adds `--read-only` flag when advisory is set.
- **Prompt annotation**: `AdvisoryEnforcer.annotate_prompt()` adds read-only instructions — defense in depth, not the sole enforcement.
- Task 2 depends on Task 3's `service_account` passthrough being in place.

Integration test: prove an advisory step cannot take a write path (spawned with read-only SA, not just annotated prompt).

### Notification Delivery Contract

Notification is **best-effort with possible duplicates**:
- Single attempt, no retry. Temporal's activity retry policy: `maximum_attempts=1`.
- Duplicate window: if the worker crashes after sending the notification but before Temporal records the activity completion, a replay may re-send the notification. This is an accepted trade-off — true at-most-once would require a persisted send marker, which is not worth the complexity for approval notifications.
- Each notification includes a stable `correlation_id` (`{workflow_id}:{step_name}`) so receivers can deduplicate if needed.
- Tests verify: notification sent, correlation_id present, no retry on failure.

### Secret-Safe Delivery Config Contract

`notifier_config` and `escalation_config` on `WorkflowInput` are **references**, not raw secrets:
- `notifier_config: {"type": "slack", "config_ref": "slack-approval-channel"}` — the activity resolves the ref to actual webhook URL/token from env vars or K8s Secrets at runtime.
- `escalation_config: {"type": "webhook", "config_ref": "escalation-endpoint"}` — same pattern.
- Secret resolution happens in the activity (worker process), never serialized in Temporal payloads.
- Config refs are mapped to env vars: `NOTIFIER_SLACK_<REF>_WEBHOOK_URL`, `ESCALATION_WEBHOOK_<REF>_URL`.

### Durable Escalation Artifact Contract

Escalation artifacts are stored in two places:
1. **Primary (always)**: The escalation `StepResult` is stored in workflow state via `self._steps["escalation"]`. This is queryable via the `GET /v1/workflows/{id}` status endpoint — the handoff payload is always retrievable.
2. **Secondary (best-effort)**: `EscalationPackager` delivers to external systems (log, webhook, Jira). Delivery failure is non-fatal because the primary copy is already in Temporal workflow state.

Test: packager delivery fails but escalation payload remains in workflow status query.

### Permissions Scope Contract

Task 3 is narrowed to what's implementable now:
- **`service_account`**: Passed to spawner, used for pod RBAC. Implemented.
- **`timeout_seconds`**: Overrides HTTP client timeout in activity. Implemented.
- **Tool filtering**: Deferred. Request-level tool filtering requires a sandbox contract change (the sandbox currently hardcodes its tool set). Filed as a future prerequisite, not a Phase 2 deliverable. Task 3 tests only assert `service_account` and `timeout_seconds`.

### Skills Image Loading Contract

- **K8s**: Init container pulls OCI image, copies `skills_paths` to shared emptyDir volume. This is the primary mechanism.
- **Podman**: `podman volume create` + `podman run --rm -v` to extract skills from OCI image into a named volume. The named volume is then mounted into the agent container. This replaces the earlier `podman cp` approach and aligns with OCI image volume semantics. Cleanup: named volume is removed in the `finally` block alongside pod destruction.
- Parity: both mechanisms deliver skills files at `/app/skills` in the agent container.

## Tasks

### Task 1: Wire auto-approval into `_handle_approval()`

Import `classify_step_risk`, `ApprovalPolicy` via `workflow.unsafe.imports_passed_through()`. Add `approval_policy` to `WorkflowInput`. In `_handle_approval()`: reconstruct `WorkflowStepSpec`, classify risk, auto-approve if safe. Emit `step.auto_approved` event.

Tests: low-risk auto-approves, high-risk waits, no risk_level defaults to manual, custom policy.

### Task 2: Wire advisory mode into workflow + activity

Add `advisory: bool = False` to `WorkflowInput` and `WorkflowDefinition`. In workflow: `annotate_prompt()` before dispatch, `should_skip_approval()` for approval steps, `annotate_output()` after completion. In activity: pass `advisory` flag in sandbox request AND set `service_account` to read-only SA when advisory=true (see Advisory Enforcement Contract).

Tests: prompt annotation, approval skip, output marking, non-advisory unchanged, advisory spawns with read-only service_account.

**Depends on:** Task 3 (service_account passthrough).

### Task 3: Wire permissions into sandbox activity

Extract `permissions` from step dict in `run_sandbox_step`. Pass `service_account` to spawner. Use `timeout_seconds` override if set. Tool filtering is deferred (see Permissions Scope Contract).

Tests: service_account forwarded, timeout override, defaults when None.

### Task 4: Wire notification activity for approval pauses

New `send_approval_notification` activity using `notifier.py`. At-most-once delivery, no retry (see Notification Delivery Contract). `notifier_config` is a config ref, not raw secrets (see Secret-Safe Delivery Config Contract). Activity resolves ref to actual credentials from env vars at runtime. Each notification includes `correlation_id` for deduplication.

Tests: Slack notifier called with correlation_id, webhook called, NullNotifier default, config ref resolved from env, no retry on failure.

### Task 5: Wire escalation delivery into activity

Extend `build_escalation_activity` to use `EscalationPackager` from `escalation.py`. `escalation_config` is a config ref, resolved at runtime (see Secret-Safe Delivery Config Contract). Packager failure is non-fatal — primary copy always in workflow state (see Durable Escalation Artifact Contract).

Tests: LogPackager default, WebhookPackager called, delivery failure non-fatal but escalation in workflow state, config ref resolved from env.

### Task 6: Advisory flag on WorkflowDefinition + API propagation

`RunWorkflowRequest.advisory` overrides definition-level setting. Definition store resolution propagates advisory.

Tests: API propagates from request, from definition, request overrides definition.

**Depends on:** Task 2.

### Task 7: Skills image support in spawners

Add `skills_image`/`skills_paths` to `AgentSpawner.spawn()`. K8s: init container copies skills to emptyDir volume. Podman: named volume extraction via `podman volume create` + `podman run --rm -v` (see Skills Image Loading Contract). Activity forwards params. Cleanup: volume removed in `finally` block.

Tests: K8s init container spec, no-skills no init container, Podman named volume extraction, volume cleanup, activity forwards params.

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
Tasks 1, 3, 4, 5, 7, 8, 9  (parallel — no inter-dependencies)
     |
Task 2 (depends on Task 3 — needs service_account passthrough for advisory enforcement)
Task 6 (depends on Task 2)
     |
Task 12 (depends on Tasks 1-5)
Task 10 (depends on Tasks 1, 2, 8)
Task 11 (depends on Tasks 1-7, 9)
     |
Task 13 (depends on all)
```

## Execution Order

1. Tasks 1, 3, 4, 5, 7, 8, 9 (parallel — auto-approve, permissions, notification, escalation, skills, infra)
2. Task 2 (advisory mode — after Task 3)
3. Task 6 (advisory API — after Task 2)
4. Task 12 (integration tests — after Tasks 1-5)
5. Tasks 10, 11 (E2E tests — after infra + policy)
6. Task 13 (review)

## Verification

- `uv run pytest tests/unit/agents/ -q` — all pass
- `uv run make format && uv run make verify` — clean
- E2E: Podman compose + Kind cluster both run workflows
- Independent reviewer LGTM
