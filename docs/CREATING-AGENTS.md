# Creating Agents and Workflows

This guide shows how to create and deploy workflows on the cloud agents platform.

## What you provide

1. **A workflow definition** (YAML) — steps, prompts, output schemas, conditions, approval gates
2. **A sandbox image** — container implementing `POST /v1/agent/run` (default: lightspeed-agentic-sandbox)
3. **Skills** (optional) — domain knowledge packages as OCI images mounted at `/app/skills/`
4. **MCP servers** (optional) — external tool servers configured via the API request

## Workflow Definition

See the examples in `examples/definitions/`:
- `diagnostic-workflow.yaml` — diagnose + approve (used in DEMO.md)
- `diagnose-fix-workflow.yaml` — diagnose → approve → fix → verify
- `ephemeral-diagnose-workflow.yaml` — single diagnostic step

### Step types

| Type | Purpose |
|------|---------|
| `agent` | Spawns a sandbox container, sends prompt + context, collects structured output |
| `human-approval` | Pauses workflow, sends notification, waits for approval signal or timeout |

### Step fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique step identifier |
| `type` | Yes | `agent` or `human-approval` |
| `output_key` | Yes | Key for this step's result in workflow state |
| `prompt` | For agent | Prompt template (supports `{{ steps.X.output.Y }}` interpolation) |
| `output_schema` | No | JSON Schema for structured output |
| `timeout_seconds` | No | Max seconds for this step (default: 600 for agent, 86400 for approval) |
| `condition` | No | Skip step if expression evaluates to false |
| `risk_level` | No | `low`, `medium`, `high`, `critical` — used by auto-approve policy |
| `message` | For approval | Human-readable approval request message |
| `max_retries` | No | Number of retry attempts (default: 1) |
| `parallel_group` | No | Steps sharing the same group run concurrently |

## Submitting a Workflow

```bash
curl -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{
    "definition": { ... },
    "provider": {"name": "openai", "model": "gpt-4o-mini", "credentials_secret": "OPENAI_API_KEY"},
    "sandbox_image": "lightspeed-agentic-sandbox:latest"
  }'
```

The API request provides deployment-time configuration:
- `provider` — LLM provider, model, credentials
- `sandbox_image` — container image for agent steps
- `skills_image` / `skills_paths` — optional skills OCI image
- `mcp_servers` — optional MCP server configs
- `approval_policy` — auto-approve rules by risk level
- `workflow_id` — optional caller-supplied idempotency key

## Checking Status

```bash
curl http://localhost:8080/v1/workflows/{workflow_id}
```

## Examples

See `examples/definitions/` for working workflow YAMLs. These are validated by CI against the Pydantic schema — any new example added to that directory is automatically tested.
