# Cloud Agents Demo — Temporal + Sandbox

## Overview

This guide runs the cloud agents system end-to-end: a Temporal workflow spawns ephemeral sandbox containers that call an LLM to diagnose Kubernetes cluster issues.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Cloud Agents System                          │
│                                                                 │
│  ┌──────────────┐    gRPC    ┌──────────────────┐               │
│  │ Workflow      │ ────────→ │ Temporal Server   │               │
│  │ Runner        │           │ (orchestration)   │               │
│  │               │           └──────────────────┘               │
│  │ POST /v1/     │                                              │
│  │ workflows/run │    spawn    ┌──────────────────┐   HTTPS     │
│  │               │ ──────────→ │ Sandbox Pod      │ ──────────→ │
│  │               │             │ (ephemeral)      │             │
│  │               │    destroy  │ POST /v1/agent/  │   OpenAI    │
│  │               │ ←────────── │ run              │   API       │
│  └──────────────┘             └──────────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Podman** (with `podman machine start`)
- **Kind** (for Kubernetes target cluster)
- **OpenAI API key** in `$OPENAI_API_KEY`
- Branch: `cloud-agents-temporal`

## Quick Start

### 1. Build images

```bash
# Workflow runner
podman build -f deploy/workflow-runner/Containerfile -t workflow-runner:latest .

# Sandbox (from your fork with temporal-integration branch)
cd ../lightspeed-agentic-sandbox
git checkout temporal-integration
podman build -f Containerfile -t lightspeed-agentic-sandbox:temporal .
cd ../lightspeed-stack
```

### 2. Start Temporal Server

```bash
podman compose -f deploy/podman/docker-compose.temporal.yaml up -d temporal-db temporal-server
# Wait ~30s for Temporal to initialize
sleep 30
```

### 3. Create a target cluster with a broken deployment

```bash
# Create Kind cluster
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name demo --wait 60s

# Deploy a healthy app + a broken app
kubectl create namespace production
kubectl apply -n production -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-frontend
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web-frontend
  template:
    metadata:
      labels:
        app: web-frontend
    spec:
      containers:
        - name: web
          image: nginx:1.25
          ports:
            - containerPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-backend
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api-backend
  template:
    metadata:
      labels:
        app: api-backend
    spec:
      containers:
        - name: api
          image: nonexistent-registry.io/api-server:v2.broken
          ports:
            - containerPort: 8080
EOF

# Verify: web-frontend Running, api-backend ErrImagePull
kubectl get pods -n production
```

### 4. Run the diagnostic workflow

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" uv run python -c "
import asyncio, os
from temporalio.client import Client
from temporalio.worker import Worker
from agents.workflow.temporal_workflow import AgentWorkflow
from agents.workflow.temporal_activities import run_sandbox_step, build_escalation_activity, send_approval_notification
from agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from agents.spawner.podman_spawner import PodmanSpawner
from agents.workflow.temporal_worker import _bind_sandbox_activity

async def main():
    client = await Client.connect('localhost:7233')
    spawner = PodmanSpawner(network='podman')
    bound = _bind_sandbox_activity(spawner)

    async with Worker(client, task_queue='demo', workflows=[AgentWorkflow],
                      activities=[bound, build_escalation_activity, send_approval_notification]):

        print('Running diagnostic workflow...')
        r = await client.execute_workflow(
            AgentWorkflow.run,
            WorkflowInput(
                definition={
                    'apiVersion': 'v1', 'kind': 'AgentWorkflow',
                    'metadata': {'name': 'diagnose-production'},
                    'spec': {'steps': [
                        {
                            'name': 'diagnose',
                            'type': 'agent',
                            'output_key': 'diagnosis',
                            'prompt': 'Kubernetes cluster issue: api-backend deployment has 0/2 pods ready due to ErrImagePull (image nonexistent-registry.io/api-server:v2.broken does not exist). web-frontend is healthy (3/3 Running). Diagnose and recommend fix.',
                            'output_schema': {
                                'type': 'object',
                                'properties': {
                                    'health_status': {'type': 'string'},
                                    'root_cause': {'type': 'string'},
                                    'fix': {'type': 'string'},
                                    'severity': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']},
                                },
                                'required': ['health_status', 'root_cause', 'fix', 'severity'],
                            },
                            'timeout_seconds': 60,
                        },
                        {
                            'name': 'approve',
                            'type': 'human-approval',
                            'output_key': 'approval',
                            'message': 'Approve the recommended fix?',
                            'risk_level': 'low',
                            'timeout_seconds': 10,
                        },
                    ]},
                },
                workflow_id='wf-demo-1',
                provider=ProviderConfig(name='openai', model='gpt-4o-mini', credentials_secret='OPENAI_API_KEY'),
                sandbox_image='localhost/lightspeed-agentic-sandbox:temporal',
                approval_policy={'auto_approve_risk_levels': ['low']},
            ),
            id='wf-demo-1', task_queue='demo')

        diag = r.steps.get('diagnosis')
        if diag and diag.status == 'completed' and diag.output:
            print(f'  Health:     {diag.output.get(\"health_status\")}')
            print(f'  Root Cause: {diag.output.get(\"root_cause\")}')
            print(f'  Fix:        {diag.output.get(\"fix\")}')
            print(f'  Severity:   {diag.output.get(\"severity\")}')
        print(f'  Approval:   {r.steps.get(\"approval\", {}).status}')

asyncio.run(main())
"
```

### 5. Expected output

```
Running diagnostic workflow...
  Health:     0/2 pods ready due to ErrImagePull
  Root Cause: Container image does not exist in the specified registry.
  Fix:        Update the deployment to use a valid image...
  Severity:   high
  Approval:   completed
```

## What happens under the hood

1. **Temporal workflow starts** — `AgentWorkflow.run()` interprets the YAML definition
2. **Step 1: Diagnose** — activity spawns a Podman container from `lightspeed-agentic-sandbox:temporal`, passes the prompt + output schema, calls OpenAI, parses structured response, destroys container
3. **Step 2: Approve** — auto-approved (low risk policy) without human intervention
4. **Workflow completes** — structured diagnosis available via `GET /v1/workflows/{id}`

## Cleanup

```bash
# Stop Temporal
podman compose -f deploy/podman/docker-compose.temporal.yaml down

# Delete Kind cluster
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name demo

# Remove images (optional)
podman rmi workflow-runner:latest lightspeed-agentic-sandbox:temporal
```

## Running tests

```bash
# Unit + integration tests (no infra needed)
uv run pytest tests/unit/agents/ tests/integration/temporal/ -q

# Temporal server tests (requires running Temporal)
kubectl port-forward svc/temporal 7233:7233 &
uv run pytest tests/e2e/temporal/test_temporal_e2e.py -v

# Or use the setup script
./tests/e2e/temporal/setup-kind.sh --run
```
