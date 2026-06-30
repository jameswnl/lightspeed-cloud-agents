# Cloud Agents — Development Guide

## Architecture

Cloud Agents uses **Temporal** for workflow orchestration and **lightspeed-agentic-sandbox** for ephemeral agent execution. Do NOT reference the old architecture (WorkflowExecutor, StepDispatcher, RecoveryPoller, pydantic-ai agents, PostgreSQL persistence) — it was deleted in PoC2.

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

These fields exist in the Pydantic model but are NOT read by `temporal_workflow.py` or `temporal_activities.py`:
- `spawn` — always ephemeral in practice
- `agent` — agent registry lookup not used in Temporal path
- `spawn_config` — resource limits come from SpawnConfig defaults

Do NOT use these in examples or documentation. The test `test_example_definitions.py::test_no_dead_fields` will catch it.

## Schema Validation

- **At API submission**: `/run` endpoint validates definitions via `temporal_validation.py` (duplicate names, undefined step refs, missing fields). Returns 422 for invalid definitions.
- **At definition store**: `/definitions` POST validates via `WorkflowDefinition.model_validate()` (full Pydantic validation).
- **Example YAML files**: `tests/unit/agents/workflow/test_example_definitions.py` validates ALL workflow YAMLs in `examples/agents/definitions/` against the Pydantic model. Add new examples there and the test picks them up automatically.
- **DEMO.md inline YAML**: `tests/unit/agents/workflow/test_demo_yaml.py` extracts and validates the workflow YAML from DEMO.md. If you edit the DEMO example, this test catches schema errors.

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
| Orphan reconciliation | `temporal_entrypoint.py` | `temporal/test_startup_reconciliation.py` |
| Podman spawned-by label | `podman_spawner.py` | `spawner/test_podman_spawner.py` |
| E2E guardrails | Both spawners | `e2e/test_guardrails.py` |

## Podman Specifics

- `PodmanSpawner` rejects `mcp_secret_mounts` with `ValueError` (K8s Secrets not available)
- `PodmanSpawner` logs a warning for `credential_secret_name` (ignored on Podman)
- `list_active()` filter format: Podman needs `filters={"label": "key=value"}` (string), NOT `["key=value"]` (list). The list format silently returns empty results.
- Podman tests can take ~10 minutes due to socket initialization. This is normal.

## Testing

```bash
# Unit tests (fast, no infra)
uv run pytest tests/unit/agents/ -q

# Example YAML validation
uv run pytest tests/unit/agents/workflow/test_example_definitions.py -v

# DEMO.md YAML validation
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
- `docs/design/cloud-agents/DEMO.md` — deployment guide + diagnostic workflow example
- `docs/design/cloud-agents/architecture-visualization.html` — interactive visualization (passcode: lcs)
- `docs/design/cloud-agents/productization-roadmap.md` — P0/P1/backlog gap analysis
- `docs/design/cloud-agents/prod/implementation-plan.md` — productization task breakdown

When updating documentation:
1. Verify claims against actual code (grep for class/function names)
2. Run `test_example_definitions.py` and `test_demo_yaml.py` after editing examples
3. Check the HTML FAQ tab — every claim should be verifiable in code
4. Do NOT claim features that exist in the schema but aren't read by the Temporal workflow (see dead fields above)

## Common Mistakes

- Using `spawn: ephemeral` or `agent: diagnostic-agent` in YAML examples — these fields are dead
- Referencing `pydantic-ai` in the workflow context — the sandbox uses OpenAI agents SDK
- Claiming `PermissionScope` (allowed_tools/denied_tools) works in the workflow path — it only works in the generic agent runtime
- Using `image.repository` in Helm values — the correct path is `workflowRunner.image.repository`
- Using `app=temporal` as a K8s label selector — the actual label is `app=temporal-server`
- Podman `list_active` filter as a list instead of string — silently returns empty
