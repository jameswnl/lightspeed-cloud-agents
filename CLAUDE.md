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
| OpenShellSpawner | `spawner/openshell_spawner.py` | Sole ephemeral spawner (issue #198) -- talks to an OpenShell gateway over gRPC, fully gateway-mediated |
| Spawner ABC | `spawner/base.py` | spawn/destroy/wait_ready/list_active + SpawnConfig validation |

### What the workflow YAML controls vs what the API request controls

**Workflow YAML** (`definition` field) defines *what*:
- Steps: name, type (agent/human-approval), prompt, output_key, output_schema
- Conditions, timeout_seconds, risk_level, max_retries, parallel_group

**API request** (`RunWorkflowRequest`) provides *how*:
- provider (name, model, credentials_secret)
- sandbox_image, skills_image, skills_paths (`skills_image`/`skills_paths` are dead -- see below)
- mcp_servers, approval_policy, notifier_config, escalation_config
- workflow_id (optional, for idempotency)

### Dead fields in WorkflowStepSpec

These fields exist in the Pydantic model but are NOT read by the workflow engine:
- `agent` — agent registry lookup not used
- `spawn_config` — resource limits come from SpawnConfig defaults

Do NOT use these in examples or documentation. The test `test_no_dead_fields` will catch it.

### Dead fields in RunWorkflowRequest

- `skills_image` / `skills_paths` — accepted for backward compatibility but ignored by `OpenShellSpawner` (logs a warning if set). This mount-and-extract mechanism was removed in issue #202 in favor of skills baked into the sandbox image plus per-step `allowed_skills`; it became fully dead once `KubernetesSpawner`/`PodmanSpawner` (the only spawners that ever read it) were deleted in issue #198. Do NOT use these in examples or documentation — use per-step `allowed_skills` instead.

### Active fields: spawn mode

The `spawn` field controls step execution isolation, and applies identically under both the local (`LocalWorkflowRunner`) and Temporal (`TemporalWorkflowRunner`) engines (issue #228 — before that, Temporal ignored `spawn` entirely and always ran the sandbox path):
- `none` — direct LLM call via pydantic-ai `model_request`, no tools, runs in-process
- `local` — LLM call via pydantic-ai in a forked subprocess, process-level isolation
- `ephemeral` — full OpenShell sandbox container (default)

The Agent path (`Agent.run()`) is used when any of: `tools`, `mcp_servers`, or `CLOUD_AGENTS_SKILLS_PATHS` are present. Without any of these, a single `model_request()` call is made.

Both engines dispatch through the same `get_step_executor()` (`workflow/executor/step/dispatch.py`) to `DirectExecutor`/`SubprocessExecutor`/`SandboxExecutor`. Under Temporal, `none`/`local` run inside `_run_sandbox_step_inner` (`temporal/activities.py`) — i.e. **on the worker process itself, not in a sandbox** — but the *activity type* scheduled by the workflow is always `run_sandbox_step` regardless of spawn mode; the branch happens inside the activity implementation, not in `_handle_agent_step()`'s `execute_activity()` call. This is deliberate: Temporal replay keys on activity type name + schedule order, not on the activity body, so branching inside the activity is replay-safe across in-flight workflow histories, while scheduling a different activity name per spawn mode would not be (would need `workflow.patched()` guarding, which this codebase doesn't use).

Caveats that apply to Temporal's `none`/`local` the same way they already applied to the local engine's:
- No `SpawnConfig` CPU/memory resource caps for `spawn: local`'s subprocess — unlike ephemeral sandboxes, a runaway subprocess isn't capped.
- `ensure_credentials_env()` (`step/provider.py`) mutates `os.environ` with first-key-wins, no locking — a real risk under Temporal's concurrent activities (default `MAX_CONCURRENT_ACTIVITIES=10`) sharing one worker process: two steps for different providers running concurrently can race on which provider's API key ends up in the shared environment.

### Tool registration

Tools are Python functions registered via `@step_tool` or `register_tool()` from `cloud_agents.workflow.executor.step.tools`. For `spawn: local`, the child subprocess needs to reconstruct the registry — set `CLOUD_AGENTS_TOOLS_MODULE` to the dotted import path of the module containing tool registrations (e.g. `myapp.tools`). The module is imported at child startup, triggering `@step_tool` decorators. Without this env var, `spawn: local` steps with `tools:` will fail with "Unknown tool".

### Skills

Skills extend agent capabilities via the `pydantic-ai-skills` package (`SkillsCapability`). Set `CLOUD_AGENTS_SKILLS_PATHS` to a colon-separated list of directories containing skill subdirectories, each with a `SKILL.md` file (YAML frontmatter + instructions). Steps opt into a subset via `allowed_skills: [name, ...]` (per-step allowlist, mirrored through `ChatWorkflowConfig.allowed_skills` for chat turns and threaded as `SkillsCapability(include=...)` for `spawn: none`/`local`, and as Landlock-policed `/skills/<name>` for `spawn: ephemeral`). `None`/omitted means **no skills** (least-privilege default); `[]` also means none. If the env var is unset or the package is not installed, skills are silently skipped regardless of the allowlist.

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
| Filesystem policy (Landlock) | `openshell_spawner.py` | `spawner/test_openshell_spawner.py`, `e2e/test_guardrails.py::TestOpenShellGuardrails` |
| Credential injection (Provider API) | `openshell_spawner.py` | `spawner/test_openshell_spawner.py` -- includes `TestEnsureProviderProfile` (issue #244: OpenShell has no builtin `ProviderProfile` for "openai"/"anthropic", see `docs/testing-against-openshell-gateways.md` and `scripts/gateway-verification/verify_provider_profile_fix.py` for the full writeup and live-gateway proof) |
| MCP secret allowlist | `temporal_activities.py` | `temporal/test_activities.py` |
| Audit events | `audit.py` + `temporal_api.py` | `temporal/test_audit.py`, `temporal/test_api.py` |
| RBAC (CallerIdentity + PolicyFile) | `authorization.py` + `policy_authorizer.py` + `temporal_api.py` | `test_authorization.py`, `test_policy_authorizer.py`, `temporal/test_api.py` |
| Circuit breaker | `circuit_breaker.py` + `temporal_activities.py` | `test_circuit_breaker.py`, `temporal/test_activities.py` |
| Cleanup failure metrics | `temporal_metrics.py` + `temporal_activities.py` | `temporal/test_cleanup_metrics.py` |
| Orphan reconciliation | `temporal_entrypoint.py` + `openshell_spawner.py` | `temporal/test_startup_reconciliation.py`, `spawner/test_openshell_spawner.py` -- `OpenShellSpawner._do_list_active()`/`_do_destroy()` now query the gateway's durable `ListSandboxes`/labels (issue #224) instead of the in-memory `self._sandbox_names` dict, so orphans from a previous process instance are discoverable across a restart. `agent_name` is recovered via a `cloud-agents/agent-name` label rather than the sandbox name itself -- the gateway caps caller-supplied names at 19 characters. |
| E2E guardrails | `openshell_spawner.py` | `e2e/test_guardrails.py::TestOpenShellGuardrails`, `TestOpenShellQueryTLS` |

## Database Migrations (Alembic)

Schema changes to PostgreSQL tables (`workflow_run_state`, `step_transcripts`) are managed via Alembic with raw SQL migrations (not SQLAlchemy ORM). The stores use asyncpg directly; Alembic uses synchronous psycopg2 for migrations only.

`alembic.ini` and `alembic/versions/` live under `src/cloud_agents/_alembic/`, not the repo root (#188) — this is required for them to ship in the wheel build (`packages = ["src/cloud_agents"]` in `pyproject.toml`) and in Containerfiles that only `COPY src/`. The Alembic CLI doesn't know this by default, so pass `-c` explicitly:

```bash
# Apply all pending migrations
RUN_STATE_DB_URL=postgresql://user:pass@localhost/cloud_agents \
  uv run alembic -c src/cloud_agents/_alembic/alembic.ini upgrade head

# Show current migration revision
uv run alembic -c src/cloud_agents/_alembic/alembic.ini current

# Show migration history
uv run alembic -c src/cloud_agents/_alembic/alembic.ini history
```

Alembic is the sole schema owner (#169) — the stores no longer have a `CREATE TABLE IF NOT EXISTS` fallback. `run_alembic()` (`storage/migrate.py`) resolves `alembic.ini`'s path programmatically (relative to its own module location, so it works the same whether run from a source checkout, an editable install, or a real wheel install) and raises `RuntimeError` if it's not found, rather than silently skipping — a missing schema now fails loudly at `connect()` instead of failing later on the first query.

### Identity model (StepMetadata)

See `docs/identity-model.md` for the full identity field relationships and schema details.

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

- `docs/ARCHITECTURE.md` — system architecture (keep in sync with code)
- `docs/DEPLOYMENT.md` — deployment guide + diagnostic workflow example
- `docs/architecture-visualization.html` — interactive visualization (passcode: lcs)
- `docs/architecture-with-openshell.md` — the Cloud Agents ↔ OpenShell gateway protocol (sandbox lifecycle, credential injection, network policy, skills enforcement) and how to configure `OpenShellSpawner` for dev (no OIDC) vs prod (OIDC) auth
- `docs/tool-registry-architecture.md` — tool/skill systems compared across `spawn: none`/`local`/`ephemeral`, plus the `spawn: ephemeral` skill-enforcement mechanism (Landlock + `materialize-skills.sh`)
- `docs/sandbox-contract.md` — the HTTP contract (`POST /v1/agent/run`, event streaming) between the workflow runner and `lightspeed-agentic-sandbox` pods
- `docs/CODE-REVIEW.md` / `docs/CODE-REVIEW-PLAN.md` — full-codebase security/quality/functional review and its remediation plan; check before assuming a guardrail gap is unknown
- `docs/testing-against-openshell-gateways.md` — how to verify `OpenShellSpawner` changes against real gateways (local-infra, Kind, real OCP, hosted staging); load before trusting any spawner change that only passed mocked tests
- `docs/archived/openshell-resolved-issues-catalog.md` — catalog of real `OpenShellSpawner` bugs mocks couldn't catch (protobuf module split between `openshell_pb2`/`datamodel_pb2`, id-vs-name cross-reference semantics, the #199 credential-exposure fix chain, the #202 Landlock materialize-skills permission bug); load if debugging a similar-looking `OpenShellSpawner` failure

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
- Referencing `KubernetesSpawner`/`PodmanSpawner`, `spawner.type=kubernetes`, or `WORKFLOW_SPAWNER=kubernetes|podman` — both classes were deleted entirely in issue #198; `OpenShellSpawner` is the only ephemeral spawner, and `WORKFLOW_SPAWNER` must be `openshell` or unset (anything else fails startup, fail-closed)
- Adding fields to workflow YAML or `temporal_workflow.py`/`temporal_activities.py` without updating `WorkflowStepSpec` in `definition.py` — Pydantic v2 silently ignores extra fields, so the YAML still loads but the schema is wrong and validation is skipped for that field
