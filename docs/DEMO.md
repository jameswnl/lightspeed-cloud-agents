# Cloud Agents — Deployment & Demo

## Overview

This guide covers:
1. **Deploying the cloud agents platform** (Temporal + workflow runner) on Podman or Kubernetes
2. **Running a diagnostic workflow** that uses an LLM to diagnose cluster issues

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
│  │               │    destroy  │ POST /v1/agent/  │   LLM       │
│  │               │ ←────────── │ run              │   Provider  │
│  └──────────────┘             └──────────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Podman** with `podman machine start` (for Podman deployment)
- **Kind** + `kubectl` (for Kubernetes deployment)
- **OpenAI API key** in `$OPENAI_API_KEY`
- Branch: `cloud-agents-temporal`

## Part 1: Build Images

Both deployment targets use the same images.

```bash
# Workflow runner
podman build -f deploy/workflow-runner/Containerfile -t workflow-runner:latest .

# Sandbox (from your fork with temporal-integration branch)
cd ../lightspeed-agentic-sandbox
git checkout temporal-integration
podman build -f Containerfile -t lightspeed-agentic-sandbox:temporal .
cd ../lightspeed-stack
```

---

## Part 2: Deploy the Platform

### Option A: Podman

The docker-compose stack includes Temporal Server, PostgreSQL, Temporal UI, and the workflow runner.

```bash
# Start the full stack
podman compose -f deploy/podman/docker-compose.temporal.yaml up -d

# Wait for Temporal to initialize (~30s)
sleep 30

# Verify
curl -s http://localhost:8080/healthz
# → {"status":"ok"}

# Temporal UI available at http://localhost:8233
```

**Environment variables** (set in docker-compose.temporal.yaml):
| Variable | Value | Purpose |
|----------|-------|---------|
| `TEMPORAL_URL` | `temporal-server:7233` | Temporal gRPC address |
| `WORKFLOW_SPAWNER` | `podman` | Use PodmanSpawner |
| `SPAWNER_NETWORK` | `cloud-agents-temporal` | Podman network for sandbox containers |
| `AUTH_REQUIRED` | `false` | No auth for local dev |

To pass the LLM API key to sandbox containers, add to the workflow-runner service in docker-compose:
```yaml
    environment:
      OPENAI_API_KEY: ${OPENAI_API_KEY}
```

### Option B: Kubernetes (Kind)

```bash
# Create a Kind cluster
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name cloud-agents --wait 60s

# Load images into Kind
kind load docker-image workflow-runner:latest --name cloud-agents
kind load docker-image lightspeed-agentic-sandbox:temporal --name cloud-agents

# Deploy Temporal Server
kubectl apply -f deploy/kind/temporal.yaml
kubectl wait --for=condition=ready pod -l app=temporal-server --timeout=120s

# Create the LLM API key Secret
kubectl create secret generic llm-api-key \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"

# Deploy the workflow runner
kubectl apply -f deploy/kind/rbac.yaml
kubectl apply -f deploy/kind/workflow-runner.yaml
kubectl wait --for=condition=ready pod -l app=workflow-runner --timeout=60s

# Verify
kubectl port-forward svc/workflow-runner 8080:8080 &
curl -s http://localhost:8080/healthz
# → {"status":"ok"}
```

### Option C: Helm (production)

```bash
helm install cloud-agents deploy/helm/cloud-agents-temporal/ \
  --set workflowRunner.image.repository=quay.io/openshift-lightspeed/workflow-runner \
  --set workflowRunner.image.tag=latest \
  --set temporal.url=temporal-server:7233 \
  --set spawner.type=kubernetes \
  --set spawner.namespace=default
```

---

## Part 3: Diagnostic Workflow

This workflow uses an LLM to diagnose a broken Kubernetes deployment. It works on both Podman and Kubernetes.

### 3a. Create a broken deployment (for Kind only)

```bash
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

### 3b. Workflow definition

Save this as `diagnostic-workflow.yaml`:

```yaml
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: diagnose-production
spec:
  steps:
    - name: diagnose
      type: agent
      output_key: diagnosis
      prompt: >
        Kubernetes cluster issue: api-backend deployment has 0/2 pods ready
        due to ErrImagePull (image nonexistent-registry.io/api-server:v2.broken
        does not exist). web-frontend is healthy (3/3 Running).
        Diagnose and recommend fix.
      output_schema:
        type: object
        properties:
          health_status:
            type: string
          root_cause:
            type: string
          fix:
            type: string
          severity:
            type: string
            enum: [low, medium, high, critical]
        required: [health_status, root_cause, fix, severity]
      timeout_seconds: 60

    - name: approve
      type: human-approval
      output_key: approval
      message: Approve the recommended fix?
      risk_level: low
      timeout_seconds: 10
```

### 3c. Submit the workflow via API

```bash
# Submit the workflow (works on both Podman and K8s — just needs port 8080 forwarded)
curl -s -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{
    "definition": '"$(cat diagnostic-workflow.yaml | python3 -c 'import sys,json,yaml; print(json.dumps(yaml.safe_load(sys.stdin)))')"',
    "provider": {
      "name": "openai",
      "model": "gpt-4o-mini",
      "credentials_secret": "OPENAI_API_KEY"
    },
    "sandbox_image": "localhost/lightspeed-agentic-sandbox:temporal",
    "approval_policy": {"auto_approve_risk_levels": ["low"]},
    "workflow_id": "wf-demo-1"
  }'
# → {"workflow_id": "wf-demo-1"}
```

### 3d. Check workflow status

```bash
# Poll for completion
curl -s http://localhost:8080/v1/workflows/wf-demo-1 | python3 -m json.tool
```

### 3e. Expected output

```json
{
  "steps": {
    "diagnosis": {
      "status": "completed",
      "output": {
        "health_status": "0/2 pods ready due to ErrImagePull",
        "root_cause": "Container image does not exist in the specified registry",
        "fix": "Update the deployment to use a valid image",
        "severity": "high"
      }
    },
    "approval": {
      "status": "completed",
      "output": {
        "approved": true,
        "auto_approved": true
      }
    }
  }
}
```

### 3f. Alternative: run programmatically

For development or CI, you can run the workflow directly in Python without the API server:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" uv run python -c "
import asyncio, yaml
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

    with open('diagnostic-workflow.yaml') as f:
        definition = yaml.safe_load(f)

    async with Worker(client, task_queue='demo', workflows=[AgentWorkflow],
                      activities=[bound, build_escalation_activity, send_approval_notification]):
        r = await client.execute_workflow(
            AgentWorkflow.run,
            WorkflowInput(
                definition=definition,
                workflow_id='wf-demo-1',
                provider=ProviderConfig(name='openai', model='gpt-4o-mini', credentials_secret='OPENAI_API_KEY'),
                sandbox_image='localhost/lightspeed-agentic-sandbox:temporal',
                approval_policy={'auto_approve_risk_levels': ['low']},
            ),
            id='wf-demo-1', task_queue='demo')

        for key, step in r.steps.items():
            print(f'{key}: {step.status}')
            if step.output:
                for k, v in step.output.items():
                    print(f'  {k}: {v}')

asyncio.run(main())
"
```

## What happens under the hood

1. **API receives request** — validates definition, generates workflow_id (or uses caller-supplied), emits `workflow_started` audit event
2. **Temporal workflow starts** — `AgentWorkflow.run()` interprets the definition
3. **Step "diagnose"** — activity spawns a sandbox container (`lightspeed-agentic-sandbox`), sets `LIGHTSPEED_PROVIDER`/`LIGHTSPEED_MODEL` env vars, calls `POST /v1/agent/run` with prompt + output schema, parses structured response, destroys container
4. **Step "approve"** — auto-approved (low risk policy) without human intervention, emits `step_approved` audit event
5. **Workflow completes** — structured diagnosis queryable via `GET /v1/workflows/{id}`

## Cleanup

### Podman
```bash
podman compose -f deploy/podman/docker-compose.temporal.yaml down
```

### Kubernetes
```bash
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name cloud-agents
```

### Images (optional)
```bash
podman rmi workflow-runner:latest lightspeed-agentic-sandbox:temporal
```

## Running tests

```bash
# Unit + integration tests (no infra needed)
uv run pytest tests/unit/agents/ tests/integration/temporal/ -q

# Temporal server tests (requires running Temporal)
uv run pytest tests/e2e/temporal/test_temporal_e2e.py -v
```
