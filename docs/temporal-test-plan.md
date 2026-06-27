# Cloud Agents — Temporal Migration Test Plan

**Date**: 2026-06-26
**Approach**: Test/spec-driven. E2E tests written before implementation for each phase. Unit tests accompany code changes.

## Current Test Inventory

### Tests to Keep (still valid with Temporal)

| Category | Files | Lines | Reason |
|---|---|---|---|
| Definition/model parsing | `test_definition.py`, `test_models.py`, `test_workflow_definition.py` | ~740 | YAML schemas unchanged |
| Conditions/interpolation | `test_conditions.py`, `test_interpolation.py` | ~290 | Pure logic, used in Temporal workflow |
| Auto-approve/advisory | `test_auto_approve.py`, `test_advisory.py` | ~170 | Risk classification, tool filtering unchanged |
| Permissions | `test_permissions.py` | ~55 | Permission scoping unchanged |
| Auth/security | `test_auth.py`, `test_token_review.py`, `test_phase7_security.py` | ~465 | Auth model unchanged |
| Spawner | `test_base.py`, `test_kubernetes_spawner.py`, `test_podman_spawner.py` | ~345 | Spawner abstraction unchanged |
| Remote client | `test_remote_agent_client.py` | ~245 | HTTP client to sandbox unchanged |
| Tools/MCP | `test_tool_loader.py`, `test_tool_instrumentation.py`, `test_mcp_loader.py` | ~265 | Tool loading unchanged |
| Observability | `test_tracing.py`, `test_metrics.py`, `test_correlation.py` | ~250 | OTel tracing unchanged |
| Escalation/retry | `test_escalation.py`, `test_retry.py` | ~230 | Escalation logic reused |
| Notifications | `test_notifier.py` | ~100 | Notification unchanged |
| Events | `test_events.py` | ~70 | Event models unchanged |
| API endpoints | `test_api.py`, `test_server.py`, `test_definition_api.py` | ~800 | API layer updated, tests adapted |
| Registry | `test_registry.py` | ~50 | Registry unchanged |
| Example agents | `test_agent.py`, `test_tools.py`, `test_cluster_state.py` | ~625 | Example agents unchanged |
| BDD features | `cloud_agents.feature` + core features | ~900+ | E2E scenarios unchanged |
| **Total kept** | | **~5,600+** | |

### Tests to Delete (replaced by Temporal)

| Category | Files | Lines | Replacement |
|---|---|---|---|
| Executor | `test_executor.py`, `test_executor_dual_mode.py` | ~575 | Temporal workflow tests |
| Graph executor | `test_graph_executor.py`, `test_graph_state.py` | ~260 | Temporal parallel step tests |
| Step dispatcher | `test_step_dispatcher.py` | ~250 | Temporal activity tests |
| Advancement/recovery | `test_advancement.py`, `test_advance.py` | ~435 | Temporal crash recovery tests |
| Persistence | `test_persistence.py`, `test_postgres_persistence.py` | ~190 | Temporal handles persistence |
| Agent loop/runner | `test_agent_loop.py`, `test_generic_runner.py`, `test_run_store.py` | ~390 | Temporal activity tests |
| **Total deleted** | | **~2,100** | |

---

## Phase-by-Phase Test Specifications

### Phase 1: Sandbox Adaptations (1-2 days)

No new E2E tests — sandbox changes are trivial (env var reading). Validated by existing sandbox unit tests + manual verification.

**Unit tests (in sandbox repo):**
```
tests/unit/test_env_vars.py:
  test_lightspeed_model_env_takes_precedence_over_sdk_specific
  test_lightspeed_model_fallback_to_sdk_specific
  test_lightspeed_model_fallback_to_default
  test_lightspeed_provider_env_takes_precedence_over_agent_provider
  test_lightspeed_provider_fallback_to_agent_provider
  test_lightspeed_provider_fallback_to_claude_default
```

---

### Phase 2: Temporal + Sandbox Integration (4-6 weeks)

#### E2E Tests — Stack Integration

```gherkin
# tests/e2e/features/temporal_stack_integration.feature

Feature: Cloud Agents integrates with lightspeed-stack infrastructure

  # --- Auth ---

  Scenario: Workflow endpoints use stack's K8s auth
    Given the stack is configured with K8S auth module
    And a valid K8s ServiceAccount token
    When I POST /v1/workflows/run with the token
    Then the request is authenticated via TokenReview
    And the workflow starts

  Scenario: Workflow endpoints reject invalid tokens
    Given the stack is configured with K8S auth module
    When I POST /v1/workflows/run with an invalid token
    Then the request is rejected with 401

  Scenario: Workflow endpoints enforce authorization
    Given a user with role "viewer" (no "workflow:run" permission)
    When the user tries to POST /v1/workflows/run
    Then the request is rejected with 403

  Scenario: Approval endpoint enforces approver role
    Given a paused workflow awaiting approval
    And a user with role "viewer" (no "workflow:approve" permission)
    When the user tries to POST /v1/workflows/:id/approve
    Then the request is rejected with 403

  Scenario: Auth works with JWT module (Podman deployment)
    Given the stack is configured with JWK auth module
    And a valid JWT token with role "workflow:admin"
    When I POST /v1/workflows/run with the JWT
    Then the request is authenticated and authorized
    And the workflow starts

  Scenario: Auth disabled in dev mode
    Given the stack is configured with Noop auth module
    When I POST /v1/workflows/run without any token
    Then the workflow starts (no auth check)

  # --- Database ---

  Scenario: Workflow audit logs stored in stack database
    Given the stack's database is initialized
    When a workflow completes
    Then an AgentRun record is created in the database
    And the record includes workflow_id, step results, and timestamps

  Scenario: Approval decisions logged in stack database
    Given a workflow with a manual approval step
    When a user approves the step
    Then an ApprovalLog record is created
    And the record includes approver identity and decision

  # --- Config ---

  Scenario: Workflow config loaded from stack YAML
    Given the stack config YAML includes an "agents" section
    When the stack starts
    Then the Temporal server address is read from config
    And the sandbox image is read from config
    And the provider/model settings are read from config

  # --- Observability ---

  Scenario: Workflow metrics appear on /metrics
    Given a completed workflow
    When I GET /metrics
    Then I see agent_runs_total counter incremented
    And I see agent_step_duration_seconds histogram populated

  Scenario: Workflow traces propagate OTel context
    Given OTel tracing is configured
    When a workflow runs
    Then spans are created for: API request → Temporal workflow → activity → sandbox call
    And all spans share the same trace_id

  # --- MCP ---

  Scenario: Sandbox pod receives MCP servers from stack config
    Given MCP servers are registered in the stack config
    When a workflow step spawns a sandbox pod
    Then the pod has LIGHTSPEED_MCP_SERVERS env var
    And the env var contains the registered servers
```

#### E2E Tests — Temporal Workflow Core

```gherkin
# tests/e2e/features/temporal_workflow.feature

Feature: Temporal workflow execution with sandbox pods

  Background:
    Given a Temporal Server is running
    And a Temporal worker is registered with AgentWorkflow
    And a sandbox image is available in the cluster

  # --- Basic execution ---

  Scenario: Single-step analysis workflow completes
    Given a workflow definition with one analysis step
    When I submit the workflow via POST /workflows/run
    Then a sandbox pod is spawned
    And the sandbox receives POST /v1/agent/run with the step prompt
    And the sandbox returns structured JSON matching the output schema
    And the workflow completes with status "completed"
    And the sandbox pod is destroyed

  Scenario: Multi-step workflow passes context between steps
    Given a workflow with steps: diagnose → approve → fix
    And the approval policy auto-approves low-risk steps
    When I submit the workflow
    Then the diagnose step runs and returns a diagnosis
    And the fix step receives approvedOption in its context
    And the approvedOption contains the diagnosis output
    And the workflow completes

  Scenario: Workflow with skipped step via condition
    Given a workflow with a fix step conditioned on "steps.approval.output.approved == true"
    And the approval step returns approved=false
    When I submit the workflow
    Then the fix step is skipped
    And the workflow completes with the fix step status "skipped"

  # --- Approval gates ---

  Scenario: Workflow pauses at human-approval step
    Given a workflow with a manual-approval step (risk_level: high)
    When I submit the workflow
    Then the workflow status is "paused"
    And GET /workflows/:id returns paused_step = "approve"

  Scenario: Approval signal resumes workflow
    Given a paused workflow waiting for approval
    When I POST /workflows/:id/approve with decision "approved"
    Then the workflow resumes and executes the next step
    And the workflow completes

  Scenario: Denial signal stops workflow
    Given a paused workflow waiting for approval
    When I POST /workflows/:id/approve with decision "denied"
    Then the workflow stops with the approval step status "denied"

  Scenario: Approval timeout fails the workflow
    Given a workflow with approval timeout of 5 seconds
    And the workflow is paused at the approval step
    When 5 seconds elapse without a signal
    Then the workflow fails with timeout error

  # --- Retry and escalation ---

  Scenario: Failed step is retried by Temporal
    Given a workflow with max_retries=2 on the fix step
    And the sandbox returns a transient error on the first attempt
    When I submit the workflow
    Then the fix step is retried
    And the second attempt receives the previous failure context
    And the workflow completes on successful retry

  Scenario: Exhausted retries trigger escalation
    Given a workflow with max_retries=1 on the fix step
    And the sandbox always returns failure
    When I submit the workflow
    Then the fix step fails after 2 attempts
    And an escalation document is generated
    And the escalation contains failure history from both attempts

  # --- SSE events ---

  Scenario: SSE endpoint streams workflow events
    Given a running workflow
    When I connect to GET /workflows/:id/events
    Then I receive step.started events as each step begins
    And I receive step.completed events as each step finishes
    And I receive a workflow.completed event when done

  # --- Crash recovery ---

  Scenario: Workflow survives worker restart
    Given a workflow in progress at step 2 of 3
    When the Temporal worker process is killed
    And a new worker process starts
    Then the workflow resumes from step 2
    And previously completed steps are NOT re-executed
    And the workflow completes
```

#### E2E Tests — Cancellation and Error Handling

```gherkin
# tests/e2e/features/temporal_cancellation.feature

Feature: Workflow cancellation and error handling

  Scenario: User cancels a running workflow
    Given a workflow in progress at step 2
    When I POST /workflows/:id/cancel
    Then the workflow status becomes "cancelled"
    And the current sandbox pod is destroyed
    And no further steps execute

  Scenario: Cancelling a completed workflow returns error
    Given a completed workflow
    When I POST /workflows/:id/cancel
    Then the request returns 400 with "workflow already completed"

  Scenario: Sandbox returns 502 for infrastructure error
    Given a sandbox pod where the LLM provider is unreachable
    When the step executes
    Then the sandbox returns HTTP 502
    And the Temporal activity raises an exception
    And Temporal retries the activity

  Scenario: Sandbox returns 200 for application failure
    Given a sandbox pod where the agent runs but reports failure
    When the step executes
    Then the sandbox returns HTTP 200 with success=false
    And the activity returns StepResult(status="failed")
    And Temporal does NOT retry (application failure)

  Scenario: Temporal Server unavailability returns meaningful error
    Given the FastAPI service is running
    And the Temporal Server is unreachable
    When I POST /workflows/run
    Then the request returns 503 with "Temporal Server unavailable"

  Scenario: Sandbox image pull failure reported clearly
    Given a workflow step referencing a nonexistent sandbox image
    When the step spawns
    Then the spawner fails with a clear image pull error
    And the step status is "failed" with the error message
```

#### E2E Tests — Sandbox Integration

```gherkin
# tests/e2e/features/temporal_sandbox.feature

Feature: Temporal activity calls sandbox correctly

  Scenario: Activity sends correct request to sandbox
    Given a workflow step with instructions and output_schema
    When the step executes
    Then the sandbox receives query = interpolated prompt
    And the sandbox receives systemPrompt = step instructions
    And the sandbox receives outputSchema = step output_schema
    And the sandbox receives context with targetNamespaces

  Scenario: Activity passes approvedOption for execution steps
    Given a completed analysis step with role "analysis"
    And an execution step with role "execution"
    When the execution step runs
    Then the sandbox receives context.approvedOption matching the analysis output

  Scenario: Activity passes executionResult for verification steps
    Given a completed execution step with role "execution"
    And a verification step with role "verification"
    When the verification step runs
    Then the sandbox receives context.executionResult with success and actionsTaken

  Scenario: Content-hash naming prevents duplicate pods
    Given a workflow step that fails and is retried
    When the retry spawns a new pod
    Then the pod name matches the original (same content hash)
    And only one pod exists for this step

  Scenario: Pod cleanup on step completion
    Given a workflow step that completes successfully
    Then the sandbox pod is destroyed after the result is captured
    And no orphaned pods remain

  Scenario: Pod cleanup on step failure
    Given a workflow step that fails
    Then the sandbox pod is destroyed even on failure
    And no orphaned pods remain
```

#### E2E Tests — Edge Cases and Robustness

```gherkin
# tests/e2e/features/temporal_robustness.feature

Feature: Workflow robustness and edge cases

  Scenario: Concurrent workflows don't interfere
    Given two workflow definitions submitted simultaneously
    When both workflows run concurrently
    Then each workflow completes independently
    And sandbox pods are named uniquely (different workflow_ids in hash)
    And results are not cross-contaminated

  Scenario: Parallel group steps run concurrently
    Given a workflow with two steps in parallel_group "diagnostics"
    When the workflow runs
    Then both steps start within 2 seconds of each other
    And the next sequential step waits for both to complete

  Scenario: Orphaned pod cleanup on worker crash and replay
    Given a workflow step that spawned a sandbox pod
    When the Temporal worker crashes mid-step
    And a new worker replays the activity
    Then the spawner detects the existing pod (same content-hash name)
    And handles it idempotently (reuse or replace)
    And the workflow completes without orphaned pods

  Scenario: Analysis output schema mismatch produces clear error
    Given a workflow where the analysis step output_schema produces flat JSON
    And the execution step expects approvedOption with nested diagnosis.rootCause
    When the execution step builds sandbox context
    Then the context builder detects the schema mismatch
    And the step fails with a clear error message (not a KeyError)

  Scenario: Workflow-level timeout cancels long-running workflows
    Given a workflow with total_timeout_seconds: 120
    And a step that takes 200 seconds
    When the workflow runs
    Then the workflow is cancelled after 120 seconds
    And the current sandbox pod is destroyed
    And the workflow status is "timed_out"

  Scenario: Invalid workflow definition rejected at submission
    Given a workflow YAML with:
      | Issue |
      | Circular condition reference |
      | Undefined step in condition |
      | Duplicate output_key |
      | Missing required fields |
    When submitted via POST /definitions
    Then each is rejected with a specific validation error
    And no workflow is created
```

#### E2E Tests — Deployment Targets

```gherkin
# tests/e2e/features/temporal_kind.feature

Feature: Temporal workflow on Kind (Kubernetes)

  Background:
    Given a Kind cluster is running
    And Temporal Server is deployed via Helm
    And the sandbox image is loaded into Kind

  Scenario: Full workflow on Kind with K8s Jobs
    Given a diagnose-and-fix workflow definition
    When I submit the workflow
    Then K8s Jobs are created for each agent step
    And each Job uses the sandbox image with imagePullPolicy=Never
    And credentials are injected via K8s SecretKeyRef
    And the workflow completes

  Scenario: Per-step ServiceAccount on Kind
    Given a workflow with service_account "readonly-sa" on the analysis step
    And service_account "exec-sa" on the execution step
    When the workflow runs
    Then the analysis Job runs as "readonly-sa"
    And the execution Job runs as "exec-sa"

  Scenario: Skills OCI image volume on Kind
    Given a workflow with skills image "quay.io/test/skills:latest"
    When a step spawns
    Then the pod has an image volume mounted at /app/skills
```

```gherkin
# tests/e2e/features/temporal_podman.feature

Feature: Temporal workflow on Podman

  Background:
    Given Temporal Server is running via podman-compose
    And the sandbox image is available locally

  Scenario: Full workflow on Podman with containers
    Given a diagnose-and-fix workflow definition
    When I submit the workflow
    Then Podman containers are created for each agent step
    And credentials are injected via host environment variables
    And the workflow completes

  Scenario: Podman container cleanup
    Given a completed workflow
    Then all spawned Podman containers are removed
    And no orphaned containers remain
```

#### Unit Tests — New Temporal Code

```
tests/unit/agents/workflow/test_temporal_workflow.py:
  TestAgentWorkflow:
    test_single_step_runs_activity
    test_multi_step_sequential_execution
    test_condition_skips_step
    test_parallel_group_runs_concurrently
    test_approval_signal_resumes
    test_approval_timeout_fails
    test_denial_stops_workflow
    test_activity_error_triggers_escalation
    test_application_failure_not_retried
    test_get_status_query_returns_steps
    test_events_accumulate_in_order

tests/unit/agents/workflow/test_temporal_activities.py:
  TestRunSandboxStep:
    test_spawns_pod_calls_sandbox_destroys
    test_content_hash_name_deterministic
    test_infrastructure_error_raises_for_temporal_retry
    test_application_failure_returns_step_result
    test_context_built_with_step_roles
    test_pre_deployed_skips_spawn
    test_pod_destroyed_on_exception

  TestBuildSandboxContext:
    test_empty_context_for_first_step
    test_target_namespaces_from_step
    test_previous_attempts_from_failed_steps
    test_approved_option_from_analysis_role
    test_execution_result_from_execution_role
    test_no_context_for_steps_without_role

  TestBuildEscalationActivity:
    test_packages_all_step_results
    test_includes_failure_history
    test_includes_handoff_command

tests/unit/agents/workflow/test_temporal_worker.py:
  TestWorkerStartup:
    test_registers_workflow_and_activities
    test_connects_to_temporal_server
```

---

### Phase 3: MCP Server Support in Sandbox (1-2 weeks)

#### E2E Tests

```gherkin
# tests/e2e/features/temporal_mcp.feature

Feature: Workflow steps with MCP tool servers

  Scenario: Agent calls MCP tool server during step
    Given a workflow step with MCP server config
    And an MCP tool server running at localhost:9090
    When the step executes
    Then the sandbox pod has LIGHTSPEED_MCP_SERVERS env var set
    And the agent calls the MCP tool server
    And the tool result is included in the agent's output

  Scenario: MCP server auth via K8s Secret
    Given an MCP server requiring bearer auth
    And the auth token stored in K8s Secret "mcp-token"
    When the step executes
    Then the sandbox reads the token from the mounted secret
    And the MCP call includes the Authorization header
```

---

### Phase 4: Composability + Triggers (G3) (2-3 weeks)

#### E2E Tests

```gherkin
# tests/e2e/features/temporal_triggers.feature

Feature: Multiple workflow trigger points

  Scenario: Workflow triggered via API
    When I POST /workflows/run with a workflow definition
    Then a Temporal workflow starts
    And I receive a workflow_id in the response

  Scenario: Workflow triggered by Temporal schedule
    Given a workflow schedule with cron "0 9 * * 1-5"
    Then the workflow runs automatically on weekday mornings

  Scenario: Workflow triggered by alert webhook
    Given an alerts adapter watching Alertmanager
    When an alert fires with labels matching a workflow trigger
    Then a workflow run is created automatically
    And the alert details are passed as input_prompt

  Scenario: Chatbot invokes workflow as a tool
    Given a chatbot agent with workflow tools registered
    When the user asks "diagnose the production cluster"
    Then the chatbot calls the diagnose-and-fix workflow tool
    And the workflow runs asynchronously
    And the chatbot streams progress to the user
```

```gherkin
# tests/e2e/features/temporal_agents_as_tools.feature

Feature: Agents and workflows as composable tools

  Scenario: Workflow registered as Pydantic AI tool
    Given a workflow definition "diagnose-and-fix" in the registry
    When the registry generates tools
    Then a tool named "run_diagnose_and_fix_workflow" is available
    And the tool accepts a prompt string
    And the tool returns the workflow output

  Scenario: Agent calls another workflow as a tool
    Given an orchestrator agent with workflow tools
    When the agent decides to run the "diagnose-and-fix" workflow
    Then a Temporal workflow starts
    And the agent receives the workflow result
```

---

### Phase 5: Access Control + Escalation Handoff (G4) (2-3 weeks)

#### E2E Tests

```gherkin
# tests/e2e/features/temporal_access_control.feature

Feature: RBAC on workflow operations

  Scenario: Unauthorized user cannot trigger workflow
    Given a user with token for team "frontend"
    And a workflow restricted to team "sre"
    When the user tries to submit the workflow
    Then the request is rejected with 403

  Scenario: Authorized user can trigger workflow
    Given a user with token for team "sre"
    And a workflow allowed for team "sre"
    When the user submits the workflow
    Then the workflow starts

  Scenario: Only approvers can approve steps
    Given a paused workflow awaiting approval
    And a user without approver role
    When the user tries to approve the step
    Then the request is rejected with 403

  Scenario: Namespace-scoped visibility
    Given workflows running in namespaces "team-a" and "team-b"
    And a user scoped to namespace "team-a"
    When the user lists workflows
    Then only team-a workflows are returned
```

```gherkin
# tests/e2e/features/temporal_escalation_handoff.feature

Feature: Escalation with human-agent handoff

  Scenario: Escalation packages full context
    Given a workflow that exhausts retries
    When the escalation step runs
    Then the escalation output includes diagnosis summary
    And the escalation output includes all failed action details
    And the escalation output includes failure history
    And the escalation output includes a handoff_command

  Scenario: Escalation context downloadable via API
    Given a workflow in "escalated" status
    When I GET /workflows/:id/escalation
    Then I receive a JSON document with full context
    And the document includes the CLI handoff command

  Scenario: CLI session bootstraps from escalation context
    Given a downloaded escalation context file
    When the user runs the handoff_command
    Then the CLI session loads the workflow context
    And the user can continue investigating interactively
```

---

### Phase 6: Product Team Onboarding

#### E2E Tests — Sample Workflow Validation

```gherkin
# tests/e2e/features/temporal_onboarding.feature

Feature: Product team workflow onboarding

  Scenario: Team submits custom workflow definition
    Given a workflow YAML provided by the product team
    When submitted via POST /definitions
    Then the definition is validated and stored
    And the workflow can be run by name

  Scenario: Team's workflow runs end-to-end
    Given the team's workflow definition is registered
    And the team's skills image is available
    When the workflow runs
    Then each step uses the team's instructions and output schema
    And the results are structured per the team's schema
    And the workflow completes successfully

  Scenario: Invalid workflow definition rejected at submission
    Given a workflow YAML with circular conditions
    When submitted via POST /definitions
    Then the request is rejected with validation errors
    And no workflow is created
```

---

## Test Execution Matrix

Each phase must pass its tests on both deployment targets before merging:

| Phase | Unit Tests | E2E Kind | E2E Podman |
|---|---|---|---|
| Phase 1 | Sandbox env var tests | N/A | N/A |
| Phase 2 | Temporal workflow + activity tests | `temporal_workflow.feature`, `temporal_sandbox.feature`, `temporal_kind.feature` | `temporal_podman.feature` |
| Phase 3 | MCP loader tests | `temporal_mcp.feature` | `temporal_mcp.feature` (Podman variant) |
| Phase 4 | Trigger + tool generation tests | `temporal_triggers.feature`, `temporal_agents_as_tools.feature` | Same |
| Phase 5 | RBAC + escalation tests | `temporal_access_control.feature`, `temporal_escalation_handoff.feature` | Same |
| Phase 6 | N/A | `temporal_onboarding.feature` | Same |

## Running Tests

```bash
# Unit tests
uv run pytest tests/unit/agents/ -q

# E2E on Kind
uv run pytest tests/e2e/ -k "temporal_kind" --kind-cluster agentic

# E2E on Podman
uv run pytest tests/e2e/ -k "temporal_podman" --podman

# All E2E (both targets)
uv run pytest tests/e2e/ -k "temporal" -q

# BDD features
uv run behave tests/e2e/features/temporal_workflow.feature
```
