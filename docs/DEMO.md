# Cloud Agents Demo & Deployment Guide

## Overview

This guide walks through deploying and running the cloud agents system — two AI agents collaborating autonomously across containers to monitor, diagnose, and remediate cluster issues.

## Components

```
┌─────────────────────────────────────────────────────────────┐
│                    Cloud Agents System                       │
│                                                             │
│  ┌──────────────────┐     HTTP dispatch    ┌──────────────────┐
│  │ Monitoring Agent  │ ──────────────────→ │ Diagnostic Agent  │
│  │                   │                     │                   │
│  │ • Periodic loop   │                     │ • Request-response│
│  │   (every 30s)     │                     │ • Tools:          │
│  │ • Detection only  │                     │   - list_hosts    │
│  │ • Tool:           │                     │   - check_host    │
│  │   get_cluster_    │                     │   - get_alerts    │
│  │   summary         │                     │   - run_remediation│
│  │ • Dispatches on   │                     │ • Output validator│
│  │   high/critical   │                     │ • Produces:       │
│  │   alerts          │                     │   DiagnosticReport│
│  │ • Produces:       │                     │                   │
│  │   MonitoringResult│                     │                   │
│  └──────────────────┘                     └──────────────────┘
│         │                                         │
│         │ OpenAI API                               │ OpenAI API
│         ▼                                         ▼
│  ┌──────────────────────────────────────────────────┐
│  │              LLM Backend (gpt-4o-mini)           │
│  │         (or Ollama with qwen3.6:latest)          │
│  └──────────────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────┘
```

**Both agents run on the same generic `agent-runtime` image.** Agent identity is determined by mounted configuration:
- `agent.yaml` — instructions, tools, lifecycle, output type
- `tools/*.py` — Python tool modules
- `registry.yaml` — agent endpoint registry for dispatch

## Data Flow

```
1. DETECT
   Monitoring Agent (loop every 30s)
     → calls get_cluster_summary()
     → LLM analyzes: finds web-02 degraded (CPU 92%, app crashed)
     → returns MonitoringResult with 2 high-severity alerts

2. DISPATCH
   Monitoring Agent
     → POST /v1/run to Diagnostic Agent (async, gets 202 + run_id)
     → passes alert context in the prompt

3. INVESTIGATE
   Diagnostic Agent
     → calls list_hosts() → check_host("web-02") → get_alerts()
     → correlates: deploy v2.3.1 caused the crash

4. REMEDIATE
   Diagnostic Agent
     → calls run_remediation("web-02", "restart_service:app", "app crashed")
     → simulated cluster state mutates: web-02 → healthy

5. VERIFY
   Diagnostic Agent
     → calls check_host("web-02") → confirms status: healthy
     → returns DiagnosticReport(cluster_healthy=True, actions_taken=[...])

6. SUPPRESS REDISPATCH
   Monitoring Agent
     → receives successful dispatch result
     → calls on_dispatch_success callback → marks web-02 healthy locally
     → next loop cycle: no alerts, no redispatch
```

## Prerequisites

- **Podman** (with podman machine running)
- **OpenAI API key** (or Ollama for local LLM)
- The `cloud-agents` branch checked out

## Quick Start

### 1. Build the generic image

```bash
git checkout cloud-agents
podman build -f deploy/agent-runtime/Containerfile -t agent-runtime:latest .
```

### 2. Create a shared network

```bash
podman network create cloud-agents
```

### 3. Start the diagnostic agent

```bash
podman run -d --name diagnostic-agent \
  --network cloud-agents \
  -p 8081:8080 \
  -v $PWD/agents/definitions/diagnostic-agent.yaml:/app/agent.yaml:ro \
  -v $PWD/agents/tools/diagnostic_tools.py:/app/tools/diagnostic_tools.py:ro \
  -v $PWD/agents/registry.yaml:/app/registry.yaml:ro \
  -e OLLAMA_URL=https://api.openai.com/v1 \
  -e AGENT_MODEL=gpt-4o-mini \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e AGENT_BOOTSTRAP_MODULE=agents.diagnostic.cluster_state \
  -e AGENT_BOOTSTRAP_FUNCTION=init_scenario \
  -e AGENT_BOOTSTRAP_ARGS=bad_deploy \
  agent-runtime:latest
```

### 4. Start the monitoring agent

```bash
podman run -d --name monitoring-agent \
  --network cloud-agents \
  -p 8082:8080 \
  -v $PWD/agents/definitions/monitoring-agent.yaml:/app/agent.yaml:ro \
  -v $PWD/agents/tools/monitoring_tools.py:/app/tools/monitoring_tools.py:ro \
  -v $PWD/agents/registry.yaml:/app/registry.yaml:ro \
  -e OLLAMA_URL=https://api.openai.com/v1 \
  -e AGENT_MODEL=gpt-4o-mini \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e AGENT_BOOTSTRAP_MODULE=agents.diagnostic.cluster_state \
  -e AGENT_BOOTSTRAP_FUNCTION=init_scenario \
  -e AGENT_BOOTSTRAP_ARGS=bad_deploy \
  -e MONITOR_INTERVAL=30 \
  agent-runtime:latest
```

### 5. Verify both agents are running

```bash
curl http://localhost:8081/healthz   # {"status":"ready","agent_name":"diagnostic-agent"}
curl http://localhost:8082/healthz   # {"status":"ready","agent_name":"monitoring-agent"}
```

### 6. Watch the autonomous loop

```bash
# Wait 30 seconds for the monitoring loop to fire, then check logs:
podman logs monitoring-agent 2>&1 | grep -E "alert|dispatch|Dispatch"
# Expected: "Detected 2 critical alert(s), dispatching"
#           "Dispatch successful, run_id=..."

# Check the diagnostic agent received and processed it:
podman logs diagnostic-agent 2>&1 | grep -E "Starting|POST|async"
# Expected: "POST /v1/run HTTP/1.1 202 Accepted"
#           "Starting async run ..."
```

### 7. Poll the dispatch result

```bash
# Get the run_id from the monitoring logs, then:
curl -s http://localhost:8081/v1/runs/<run_id> | python3 -m json.tool
# Expected: status=completed, actions_taken=[restart_service, scale_resources]
```

## Manual Testing

### Direct diagnostic call

```bash
curl -s -X POST http://localhost:8081/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Check all hosts and fix anything broken"}' \
  | python3 -m json.tool
```

### Direct monitoring check

```bash
curl -s -X POST http://localhost:8082/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Check all hosts for issues"}' \
  | python3 -m json.tool
```

### Async submit + poll

```bash
# Submit async
RESPONSE=$(curl -s -X POST http://localhost:8081/v1/run \
  -H 'Content-Type: application/json' \
  -H 'Prefer: respond-async' \
  -d '{"prompt":"Full cluster diagnosis"}')
echo $RESPONSE
# {"run_id":"...","status":"running"}

# Poll
RUN_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")
curl -s http://localhost:8081/v1/runs/$RUN_ID | python3 -m json.tool
```

### Check liveness and metrics

```bash
curl http://localhost:8081/livez      # {"status":"alive"}
curl http://localhost:8081/metrics    # Prometheus format
```

## Endpoints

Each agent pod exposes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Readiness check |
| `/livez` | GET | Liveness check (detects hung agents) |
| `/v1/run` | POST | Run the agent (sync or async via `Prefer: respond-async`) |
| `/v1/runs/{run_id}` | GET | Poll async run status |
| `/metrics` | GET | Prometheus metrics (internal-only) |

## Using Ollama Instead of OpenAI

Replace the OpenAI env vars with:

```bash
-e OLLAMA_URL=http://host.containers.internal:11434/v1 \
-e AGENT_MODEL=qwen3.6:latest \
```

Make sure Ollama is running: `ollama serve`

## Cleanup

```bash
podman rm -f diagnostic-agent monitoring-agent
podman network rm cloud-agents
```

## Running Tests

```bash
# Unit tests (223 tests, no containers needed)
uv run pytest tests/unit/agents/ -v

# E2E tests (needs both agents running on ports 8081/8082)
make e2e-cloud-agents-full
```

## Simulated Cluster Scenarios

The `AGENT_BOOTSTRAP_ARGS` env var controls the initial cluster state:

| Scenario | What it simulates |
|----------|-------------------|
| `healthy` | All 4 hosts healthy, no issues |
| `bad_deploy` | web-02 degraded — app crashed after deploy v2.3.1, CPU 92% |
| `disk_growth` | db-01 disk at 82%, trending toward critical threshold |
