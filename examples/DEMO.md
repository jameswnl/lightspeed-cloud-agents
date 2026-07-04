# Demo Guide

## Recording

See [cloud-agents-demo-1.mov](cloud-agents-demo-1.mov) for a recorded walkthrough of the K8s Incident Response scenario (diagnose → approve → fix → verify).

## Dashboard

The interactive dashboard visualizes workflow execution in real-time.

```bash
make dashboard  # serves at http://localhost:3000/demo-dashboard.html
```

### Scenarios

| Scenario | Type | Description |
|----------|------|-------------|
| K8s Incident Response | Live | diagnose → approve → fix → verify with real LLM calls |
| MCP Tool Integration | Live | Agent reads files via filesystem MCP server tools |
| Multi-workflow Composition | Animated | Chatbot triggers chained workflows (future vision) |
| Security & Governance | Live | Audit → approve (critical) → remediate |

### Terminal setup for demo

**Terminal 1** — Dashboard: `make dashboard`

**Terminal 2** — Sandbox logs: `make watch-sandboxes`

**Terminal 3** — Container lifecycle: `watch -n1 podman ps --filter label=spawned-by=workflow-runner`
