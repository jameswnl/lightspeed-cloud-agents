# Lightspeed Cloud Agents

Cloud-based agent workflow platform. Deploys AI agents as ephemeral containers in Kubernetes or Podman, powered by Temporal.

## Quick Start

```bash
# Install
uv sync --group dev

# Run tests
uv run pytest tests/unit/ -q

# Start workflow runner (requires Temporal Server at localhost:7233)
uv run uvicorn cloud_agents.workflow.temporal_entrypoint:app --host 0.0.0.0 --port 8080
```

See [docs/DEMO.md](docs/DEMO.md) for full deployment guide with Podman and Kubernetes.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
Workflow Runner (FastAPI + Temporal Worker)
    │ gRPC
    ▼
Temporal Server (durable execution)
    │
    ▼
Spawner → Sandbox Pod (lightspeed-agentic-sandbox)
    │ POST /v1/agent/run
    ▼
LLM Provider (OpenAI / Vertex / Bedrock)
```
