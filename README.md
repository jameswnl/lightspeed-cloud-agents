# Lightspeed Cloud Agents

Agent workflow and harness platform. Deploys AI agents as ephemeral sandbox containers in Kubernetes or Podman, powered by Temporal.


---


## Quick Start

### Prerequisites

- **Podman** with Podman Desktop or `podman machine start`
- **LLM API key** — `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`



### Start the Cloud Agents Platform


```bash
export OPENAI_API_KEY="sk-..."    # or ANTHROPIC_API_KEY

make build      # build all 3 images (runner, sandbox, MCP server — MCP is only needed for demo)
make up         # start the platform (Temporal + runner + MCP)
make dashboard  # open demo dashboard at http://localhost:3000/demo-dashboard.html
```


### What Gets Deployed

Three images, six containers:

| Image | Purpose | Container |
|-------|---------|-----------|
| `workflow-runner` | REST API + Temporal Worker — the brain that interprets workflow YAML and dispatches steps | `podman-workflow-runner-1` |
| `lightspeed-agentic-sandbox` | Agent runtime — each workflow step spawns one of these. Runs a complete agent loop (multi-turn LLM + tool calls) then exits. | `agent-ca-*` (ephemeral) |
| `mcp-filesystem` | MCP tool server (demo only) — exposes filesystem read/write tools over streamable HTTP. Sandbox containers connect to it for tool calls. | `podman-mcp-filesystem-1` |

Plus three infrastructure containers managed by compose:

| Container | Purpose |
|-----------|---------|
| `podman-temporal-server-1` | Temporal Server — durable workflow state, retry, signals |
| `podman-temporal-db-1` | PostgreSQL — Temporal's storage backend |
| `podman-temporal-ui-1` | Temporal Web UI — workflow inspection at http://localhost:8233 |

```mermaid
graph LR
    subgraph cluster["Cloud Agents Platform"]
        WR["Workflow Runner<br/><i>API + Temporal Worker</i>"]
        TS["Temporal Server"]
        SB["Sandbox Container<br/><i>ephemeral, per step</i>"]
        MCP["MCP Server<br/><i>optional tools</i>"]
    end
    LLM["LLM Provider"]

    WR -- "gRPC" --> TS
    WR -- "spawn / destroy" --> SB
    SB -- "HTTPS" --> LLM
    SB -- "HTTP" --> MCP
```

### Try It

Register a workflow definition (this one uses MCP tools to read files — no approval needed):

```bash
python3 -c "import yaml,json,sys; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))" \
  examples/workflow-definitions/mcp-filesystem-workflow.yaml | \
  curl -s -X POST http://localhost:8080/v1/workflows/definitions \
    -H 'Content-Type: application/json' -d @-
```

List registered workflows:

```bash
curl -s http://localhost:8080/v1/workflows/definitions | python3 -m json.tool
```

Run a workflow (the agent reads cluster status files via MCP tools and recommends a fix):

```bash
curl -s -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow_name": "mcp-filesystem-demo",
    "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "OPENAI_API_KEY"},
    "sandbox_image": "lightspeed-agentic-sandbox:latest",
    "mcp_servers": [{"name": "filesystem", "url": "http://mcp-filesystem:8081/mcp"}]
  }'
# → {"workflow_id": "wf-abc123"}
```

Watch the sandbox containers spawn and execute:

```bash
# In another terminal — see containers appear and disappear
watch podman ps --filter label=spawned-by=workflow-runner

# Tail the agent loop logs inside a sandbox
podman logs -f $(podman ps --filter label=spawned-by=workflow-runner --format '{{.Names}}' | head -1)
```

Check workflow result:

```bash
curl -s http://localhost:8080/v1/workflows/<workflow_id> | python3 -m json.tool
```

You can also open the Temporal UI at http://localhost:8233 to inspect workflow runs, event history, and step state.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full API reference, demo dashboard, and Kubernetes deployment.


---


## Development

Run the workflow runner locally (without containers) for development and debugging.

```bash
# Install dependencies
uv sync --group dev --extra podman

# Start Temporal (still needs containers)
podman compose -f deploy/podman/docker-compose.temporal.yaml up -d temporal-db temporal-server

# Run the workflow runner on the host
TEMPORAL_URL=localhost:7233 \
WORKFLOW_SPAWNER=podman \
SPAWNER_NETWORK=podman_default \
AUTH_REQUIRED=false \
uv run uvicorn cloud_agents.workflow.temporal_entrypoint:app --host 0.0.0.0 --port 8080
```

Run tests:

```bash
uv run pytest tests/unit/ -q             # unit tests
uv run pytest tests/integration/ -v      # integration tests (requires Temporal — see Quick Start)
```

---


## Key Docs

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — goals, requirements, design, components
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — deployment guide (Podman / Kind / Helm) + workflow definition reference + diagnostic workflow example
- [RBAC](docs/rbac.md) — authorization: policy file format, identity matching, quick start
- [Implementation Plan](docs/gaps/gaps-implementation-plan.md) — all planned work (T1-T50)
