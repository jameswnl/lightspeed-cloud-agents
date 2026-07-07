# Demo Guide

## Setup

```bash
make demo-up     # build images + start platform + MCP server + CORS
make dashboard   # open http://localhost:3000/demo-dashboard.html
```

The demo stack adds an MCP filesystem server and CORS support on top of the core platform. See [README Quick Start](../README.md#quick-start) for core-only setup.

## Recording

See [cloud-agents-demo-1.mov](cloud-agents-demo-1.mov) for a recorded walkthrough of the K8s Incident Response scenario (diagnose → approve → fix → verify).

## Dashboard

The interactive dashboard visualizes workflow execution in real-time. Select a scenario and click Run.

### Scenarios

| Scenario | Type | Description |
|----------|------|-------------|
| K8s Incident Response | Live | diagnose → approve → fix → verify with real LLM calls |
| MCP Tool Integration | Live | Agent reads files via filesystem MCP server tools |
| Multi-workflow Composition | Animated | Chatbot triggers chained workflows (future vision) |
| Security & Governance | Live | RBAC, approval gates, audit trail |

### Real Cluster Mode (K8s Incident Response)

Scenario 1 supports two modes, selectable via a toggle in the dashboard:

- **Simulated** (default): The agent generates text about a hardcoded ErrImagePull issue. No real cluster is needed.
- **Real Cluster**: The agent uses kubectl MCP tools to interact with a live Kubernetes cluster. Requires:
  - The mcp-kubectl service running (included in `make demo-up`)
  - A reachable cluster: either a Kind cluster (`make kind-up`) or a kubeconfig mounted into the mcp-kubectl container
  - If no cluster is reachable, the agent will report that tools are unavailable (graceful degradation)

The toggle state persists in the browser via localStorage.

### Terminal setup for demo

**Terminal 1** — Dashboard: `make dashboard`

**Terminal 2** — Sandbox logs: `make watch-sandboxes`

**Terminal 3** — Container lifecycle: `watch -n1 podman ps --filter label=spawned-by=workflow-runner`
