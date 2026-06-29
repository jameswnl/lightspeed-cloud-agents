# Temporal Dev Setup

## Quick Start (Temporal Lite)

The fastest way to run Temporal locally for development:

```bash
# Install Temporal CLI
brew install temporal

# Start dev server (SQLite, no external dependencies)
temporal server start-dev

# Temporal is now at localhost:7233
# Web UI at http://localhost:8233
```

This gives you a single-binary Temporal server with SQLite storage. No PostgreSQL, no Docker containers. Suitable for local development and running the test suite.

## Running the Workflow Runner Locally

```bash
# Set Temporal connection
export TEMPORAL_URL=localhost:7233
export TEMPORAL_NAMESPACE=default

# Start the workflow runner
uv run uvicorn agents.workflow.temporal_entrypoint:app --host 0.0.0.0 --port 8080
```

## Running Tests

### Unit + Integration Tests (no Temporal Server needed)

```bash
uv run pytest tests/unit/agents/ -q                    # 399 unit tests
uv run pytest tests/integration/temporal/ -v            # 3 integration tests (uses WorkflowEnvironment)
```

### Temporal Server Tests (requires running server)

```bash
# Option 1: Temporal Lite
temporal server start-dev &

# Option 2: Kind cluster
./tests/e2e/temporal/setup-kind.sh

# Option 3: Podman compose
podman compose -f deploy/podman/docker-compose.temporal.yaml up -d temporal-db temporal-server

# Run tests
uv run pytest tests/e2e/temporal/ -v                   # 9 Temporal server tests
```

## Deployment Options

| Environment | How | Config |
|-------------|-----|--------|
| **Dev (local)** | `temporal server start-dev` | SQLite, single binary |
| **Dev (Podman)** | `docker-compose.temporal.yaml` | PostgreSQL, full stack |
| **Kind cluster** | `deploy/kind/temporal.yaml` | PostgreSQL, K8s manifests |
| **Production** | Temporal Cloud or self-hosted | PostgreSQL, HA setup |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPORAL_URL` | `localhost:7233` | Temporal Server gRPC address |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `WORKFLOW_SPAWNER` | (none) | `kubernetes` or `podman` |
| `SPAWNER_NAMESPACE` | `default` | K8s namespace for spawned pods |
| `AUTH_REQUIRED` | `false` | Fail startup if auth init fails |
