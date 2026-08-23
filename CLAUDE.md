# Cloud Agents — Development Guide

## Architecture

Cloud Agents uses **Temporal** or **pydantic-graph** (local runner) for workflow execution, **pydantic-ai** for LLM calls in spawn: none/local steps, and **lightspeed-agentic-sandbox** for ephemeral agent execution. Do NOT reference the old architecture (StepDispatcher, RecoveryPoller, PostgreSQL persistence) — it was deleted in PoC2. The workflow runner interface is `WorkflowRunner` (ABC) with `LocalWorkflowRunner` (pydantic-graph) and `TemporalWorkflowRunner` (Temporal SDK) implementations.

### Key components

| Component | File | Purpose |
|-----------|------|---------|
| AgentWorkflow | `workflow/temporal_workflow.py` | Temporal `@workflow.defn` — interprets YAML definitions |
| Sandbox activity | `workflow/temporal_activities.py` | Spawns sandbox, calls POST /v1/agent/run, destroys |
| API | `workflow/temporal_api.py` | REST endpoints: /run, /approve, /{id}, /definitions |
| Entrypoint | `workflow/temporal_entrypoint.py` | FastAPI app with Temporal Worker lifespan |
| Models | `workflow/temporal_models.py` | ProviderConfig, WorkflowInput, MCPServerConfig, StepResult |
| KubernetesSpawner | `spawner/kubernetes_spawner.py` | K8s Jobs with scoped SAs, securityContext, Secret mounts |
| PodmanSpawner | `spawner/podman_spawner.py` | Podman containers with network config |
| Spawner ABC | `spawner/base.py` | spawn/destroy/wait_ready/list_active + SpawnConfig validation |

### What the workflow YAML controls vs what the API request controls

**Workflow YAML** (`definition` field) defines *what*:
- Steps: name, type (agent/human-approval), prompt, output_key, output_schema
- Conditions, timeout_seconds, risk_level, max_retries, parallel_group

**API request** (`RunWorkflowRequest`) provides *how*:
- provider (name, model, credentials_secret)
- sandbox_image, skills_image, skills_paths
- mcp_servers, approval_policy, notifier_config, escalation_config
- workflow_id (optional, for idempotency)

### Dead fields in WorkflowStepSpec

These fields exist in the Pydantic model but are NOT read by the workflow engine:
- `agent` — agent registry lookup not used
- `spawn_config` — resource limits come from SpawnConfig defaults

Do NOT use these in examples or documentation. The test `test_no_dead_fields` will catch it.

### Active fields: spawn mode

The `spawn` field controls step execution isolation:
- `none` — direct LLM call via pydantic-ai `model_request`, no tools, runs in-process
- `local` — LLM call via pydantic-ai in a forked subprocess, process-level isolation
- `ephemeral` — full OpenShell sandbox container (default)

The Agent path (`Agent.run()`) is used when any of: `tools`, `mcp_servers`, or `CLOUD_AGENTS_SKILLS_PATHS` are present. Without any of these, a single `model_request()` call is made.

### Tool registration

Tools are Python functions registered via `@step_tool` or `register_tool()` from `cloud_agents.workflow.executor.step.tools`. For `spawn: local`, the child subprocess needs to reconstruct the registry — set `CLOUD_AGENTS_TOOLS_MODULE` to the dotted import path of the module containing tool registrations (e.g. `myapp.tools`). The module is imported at child startup, triggering `@step_tool` decorators. Without this env var, `spawn: local` steps with `tools:` will fail with "Unknown tool".

### Skills

Skills extend agent capabilities via the `pydantic-ai-skills` package (`SkillsCapability`). Set `CLOUD_AGENTS_SKILLS_PATHS` to a colon-separated list of directories containing skill subdirectories, each with a `SKILL.md` file (YAML frontmatter + instructions). The capability is automatically passed to the pydantic-ai Agent for **every** `spawn: none` and `spawn: local` step — there is no per-step allowlist (unlike `tools:`). If the env var is unset or the package is not installed, skills are silently skipped.

**Trust model:** `SkillsCapability` registers `run_skill_script` which can execute arbitrary Python from skill directories. Only configure `CLOUD_AGENTS_SKILLS_PATHS` to directories owned by trusted users. Do not point at world-writable or user-uploaded paths.

## Schema Validation

- **At API submission**: `/run` endpoint validates definitions via `temporal_validation.py` (duplicate names, undefined step refs, missing fields). Returns 422 for invalid definitions.
- **At definition store**: `/definitions` POST validates via `WorkflowDefinition.model_validate()` (full Pydantic validation).
- **Example YAML files**: `tests/unit/agents/workflow/test_example_definitions.py` validates ALL workflow YAMLs in `examples/workflow-definitions/` against the Pydantic model. Add new examples there and the test picks them up automatically.
- **DEPLOYMENT.md inline YAML**: `tests/unit/agents/workflow/test_demo_yaml.py` extracts and validates the workflow YAML from DEPLOYMENT.md. If you edit the DEMO example, this test catches schema errors.

## Security Guardrails

All implemented guardrails have corresponding tests. When adding a new guardrail, add the test first.

| Guardrail | Where enforced | Test file |
|-----------|---------------|-----------|
| risk_level (fails closed to "high") | `auto_approve.py` | `test_auto_approve.py` |
| Approval gates | `temporal_workflow.py` | `temporal/test_workflow.py` |
| Advisory mode (read-only fs) | `temporal_activities.py` | `temporal/test_activities.py` |
| Hard timeouts | `temporal_workflow.py` | `temporal/test_workflow.py` |
| Resource limits (SpawnConfig) | `spawner/base.py` | `spawner/test_base.py` |
| Concurrency cap | `spawner/base.py` | `spawner/test_base.py` |
| securityContext | `kubernetes_spawner.py` | `spawner/test_kubernetes_spawner.py` |
| Credential Secret mount | `kubernetes_spawner.py` | `spawner/test_kubernetes_spawner.py` |
| MCP secret allowlist | `temporal_activities.py` | `temporal/test_activities.py` |
| Audit events | `audit.py` + `temporal_api.py` | `temporal/test_audit.py`, `temporal/test_api.py` |
| RBAC (CallerIdentity + PolicyFile) | `authorization.py` + `policy_authorizer.py` + `temporal_api.py` | `test_authorization.py`, `test_policy_authorizer.py`, `temporal/test_api.py` |
| Circuit breaker | `circuit_breaker.py` + `temporal_activities.py` | `test_circuit_breaker.py`, `temporal/test_activities.py` |
| Cleanup failure metrics | `temporal_metrics.py` + `temporal_activities.py` | `temporal/test_cleanup_metrics.py` |
| Orphan reconciliation | `temporal_entrypoint.py` | `temporal/test_startup_reconciliation.py` |
| Podman spawned-by label | `podman_spawner.py` | `spawner/test_podman_spawner.py` |
| E2E guardrails | Both spawners | `e2e/test_guardrails.py` |

## Podman Specifics

- `PodmanSpawner` rejects `mcp_secret_mounts` with `ValueError` (K8s Secrets not available)
- `PodmanSpawner` logs a warning for `credential_secret_name` (ignored on Podman)
- `list_active()` filter format: Podman needs `filters={"label": "key=value"}` (string), NOT `["key=value"]` (list). The list format silently returns empty results.
- Podman tests can take ~10 minutes due to socket initialization. This is normal.

## Database Migrations (Alembic)

Schema changes to PostgreSQL tables (`workflow_run_state`, `step_transcripts`) are managed via Alembic with raw SQL migrations (not SQLAlchemy ORM). The stores use asyncpg directly; Alembic uses synchronous psycopg2 for migrations only.

```bash
# Apply all pending migrations
RUN_STATE_DB_URL=postgresql://user:pass@localhost/cloud_agents uv run alembic upgrade head

# Show current migration revision
uv run alembic current

# Show migration history
uv run alembic history
```

The `CREATE TABLE IF NOT EXISTS` in each store's `connect()` is kept for backward compatibility during migration rollout.

### Identity model (StepMetadata)

`StepMetadata` on `StepInput` carries cross-cutting identity fields:
- `user_id` — who initiated the workflow
- `session_id` — groups related workflows (caller-provided)
- `trace_id` — OTEL trace ID for correlation
- `conversation_id` — conversation/workflow ID for chat mode
- `extra` — extension dict for consumer-specific data

`ConversationMessage` (`step/conversation.py`) is our framework-agnostic message format, stored in the `messages` JSONB column on `step_transcripts`.

### Store methods for identity queries

- `RunStateStore.list_by_user(user_id)` — all workflows for a user
- `RunStateStore.list_by_session(session_id)` — all workflows in a session
- `TranscriptStore.load_recent_turns(workflow_id, limit)` — recent conversation turns

## Testing

```bash
# Unit tests (fast, no infra)
uv run pytest tests/unit/agents/ -q

# Example YAML validation
uv run pytest tests/unit/agents/workflow/test_example_definitions.py -v

# DEPLOYMENT.md YAML validation
uv run pytest tests/unit/agents/workflow/test_demo_yaml.py -v

# E2E guardrails (requires Podman running)
uv run pytest tests/e2e/test_guardrails.py -v -k podman

# E2E guardrails (requires Kind cluster)
uv run pytest tests/e2e/test_guardrails.py -v -k kind

# Temporal integration (requires Temporal Server)
uv run pytest tests/e2e/temporal/test_temporal_e2e.py -v
```

## Documentation

- `docs/design/cloud-agents/ARCHITECTURE.md` — system architecture (keep in sync with code)
- `docs/design/cloud-agents/DEPLOYMENT.md` — deployment guide + diagnostic workflow example
- `docs/design/cloud-agents/architecture-visualization.html` — interactive visualization (passcode: lcs)
- `docs/design/cloud-agents/productization-roadmap.md` — P0/P1/backlog gap analysis
- `docs/design/cloud-agents/prod/implementation-plan.md` — productization task breakdown

When updating documentation:
1. Verify claims against actual code (grep for class/function names)
2. Run `test_example_definitions.py` and `test_demo_yaml.py` after editing examples
3. Check the HTML FAQ tab — every claim should be verifiable in code
4. Do NOT claim features that exist in the schema but aren't read by the Temporal workflow (see dead fields above)

## Common Mistakes

- Using `agent: some-agent` in YAML examples — this field is dead (spawn is now active with values none/local/ephemeral)
- Claiming tools work without registration — tools must be registered via `register_tool()` or `@step_tool` before they can be used in workflow steps
- Using `spawn: local` + `tools:` without setting `CLOUD_AGENTS_TOOLS_MODULE` — the child process has an empty registry; set the env var to the module containing `@step_tool` registrations
- Claiming `PermissionScope` (allowed_tools/denied_tools) is fully enforced — the runner forwards `allowedTools`/`deniedTools` in the sandbox POST body, but sandbox-side enforcement is pending (separate repo: lightspeed-agentic-sandbox)
- Using `image.repository` in Helm values — the correct path is `workflowRunner.image.repository`
- Using `app=temporal` as a K8s label selector — the actual label is `app=temporal-server`
- Podman `list_active` filter as a list instead of string — silently returns empty
- Adding fields to workflow YAML or `temporal_workflow.py`/`temporal_activities.py` without updating `WorkflowStepSpec` in `definition.py` — Pydantic v2 silently ignores extra fields, so the YAML still loads but the schema is wrong and validation is skipped for that field
