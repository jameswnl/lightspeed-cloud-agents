# Lightspeed Cloud Agents

AI agent workflow platform. Define multi-step agent workflows in YAML, run them in ephemeral sandbox containers on Kubernetes or Podman, with human approval gates and durable execution via Temporal.


---


## Quick Start

### Prerequisites

- **Podman** with Podman Desktop or `podman machine start`
- **LLM API key** — `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`



### Start the Cloud Agents Platform


```bash
export OPENAI_API_KEY="sk-..."    # or ANTHROPIC_API_KEY

make build      # build 2 images (runner + sandbox)
make up         # start the platform (Temporal + runner)
```

For the interactive demo dashboard (includes MCP server):

```bash
make build-all  # build all 3 images (adds MCP server)
make demo-up    # start platform + MCP server + CORS
make dashboard  # open demo dashboard at http://localhost:3000/demo-dashboard.html
```


### What Gets Deployed

**Core** (`make up`) — 4 containers:

| Container | Purpose |
|-----------|---------|
| `podman-workflow-runner-1` | REST API + Temporal Worker — interprets workflow YAML, dispatches steps |
| `podman-temporal-server-1` | Temporal Server — durable workflow state, retry, signals |
| `podman-temporal-db-1` | PostgreSQL — Temporal's storage backend |
| `podman-temporal-ui-1` | Temporal Web UI — http://localhost:8233 |

Plus ephemeral `agent-ca-*` sandbox containers spawned per workflow step (complete agent loop: multi-turn LLM + tool calls, then destroyed).

**Demo** (`make demo-up`) adds:

| Container | Purpose |
|-----------|---------|
| `podman-mcp-filesystem-1` | MCP tool server — filesystem tools over streamable HTTP |

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

Register a workflow definition (single diagnostic step — no MCP, no approval):

```bash
python3 -c "import yaml,json,sys; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))" \
  examples/workflow-definitions/ephemeral-diagnose-workflow.yaml | \
  curl -s -X POST http://localhost:8080/v1/workflows/definitions \
    -H 'Content-Type: application/json' -d @-
```

List registered workflows:

```bash
curl -s http://localhost:8080/v1/workflows/definitions | python3 -m json.tool
```

Run a workflow:

```bash
curl -s -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow_name": "ephemeral-diagnose",
    "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "OPENAI_API_KEY"},
    "sandbox_image": "lightspeed-agentic-sandbox:latest"
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
make test-unit                           # unit tests (no infra needed)
uv run pytest tests/integration/ -v      # integration tests (requires Temporal — see Quick Start)
```

---


## Key Docs

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — goals, requirements, design, components
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — deployment options (Podman / Kind / Helm), API reference, workflow definition schema
- [DEMO.md](examples/DEMO.md) — demo dashboard, recording, terminal setup
- [RBAC](docs/rbac.md) — authorization: policy file format, identity matching, quick start
- [Implementation Plan](docs/gaps/gaps-implementation-plan.md) — all planned work (T1-T50)
