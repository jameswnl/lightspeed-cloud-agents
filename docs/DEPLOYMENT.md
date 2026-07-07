# Cloud Agents — Deployment & Demo

For quick start, see [README.md](../README.md#quick-start).

For day-2 operations and troubleshooting, see the [Operational Runbook](operations/runbook.md).


## Building Images

```bash
make build          # builds 2 core images (runner + sandbox)
make build-demo     # builds all 3 images (adds MCP server for demo)
make build-runner   # just the workflow runner
make build-sandbox  # just the sandbox (clones fork if needed)
make build-mcp      # just the MCP filesystem server (demo only)
```

The sandbox image must be built from our fork ([jameswnl/lightspeed-agentic-sandbox @ temporal-integration](https://github.com/jameswnl/lightspeed-agentic-sandbox/tree/temporal-integration)) which has MCP streamable HTTP support. `make build-sandbox` handles cloning and checkout automatically.

---

## Deploying the Platform

### Option A: Podman (compose)

See [Quick Start](../README.md#quick-start).

### Option B: Kubernetes (Kind)

```bash
export OPENAI_API_KEY="sk-..."

make kind-up    # creates cluster, loads images, deploys Temporal + runner

kubectl port-forward svc/workflow-runner 8080:8080 &
curl -s http://localhost:8080/readyz

make kind-down  # delete cluster
```

### Option C: Helm (production)

```bash
helm install cloud-agents deploy/helm/ \
  --set workflowRunner.image.repository=quay.io/openshift-lightspeed/workflow-runner \
  --set workflowRunner.image.tag=latest \
  --set temporal.url=temporal-server:7233 \
  --set spawner.type=kubernetes
```

---

## Network Egress Policy

Sandbox containers are restricted to outbound DNS and explicitly configured LLM provider endpoints by default. This prevents a compromised or malicious agent from exfiltrating data to arbitrary hosts.

### Kubernetes (Helm)

Egress enforcement is **enabled by default** in the Helm chart. The chart creates two egress NetworkPolicies:

1. **Workflow runner egress** — allows Temporal gRPC (7233), sandbox HTTP (8080), K8s API (443), and DNS (53).
2. **Sandbox egress** — allows DNS (53) and HTTPS (443) to explicitly configured LLM provider CIDRs only.

Configure `llmCidrs` with your LLM provider IP ranges:

```yaml
networkPolicy:
  enabled: true
  egress:
    enabled: true
    llmCidrs:
      - "13.107.238.0/24"    # example: Azure OpenAI
      - "35.199.224.0/19"    # example: Google Vertex AI
```

To find your provider's IP ranges:
- **OpenAI**: check [OpenAI platform status](https://status.openai.com) or resolve `api.openai.com`
- **Azure OpenAI**: use your endpoint's IP range from Azure IP ranges JSON
- **Google Vertex AI**: use `us-central1-aiplatform.googleapis.com` resolved IPs

If `llmCidrs` is empty, sandbox pods can only reach DNS — LLM calls will fail. This is intentional: deployers must explicitly configure which endpoints agents can reach.

To disable egress enforcement (not recommended):

```yaml
networkPolicy:
  egress:
    enabled: false
```

### Kubernetes (Kind)

Kind deployments apply `deploy/kind/network-policy.yaml` automatically via `make kind-up`. The Kind egress policy allows all HTTPS (port 443) egress from sandbox pods for development convenience. Production deployments should use Helm with explicit `llmCidrs`.

### Podman

Podman does not support NetworkPolicy. For equivalent egress protection, configure host firewall rules (iptables or nftables).

**iptables example** — restrict the Podman network interface to DNS and specific LLM endpoints:

```bash
# Identify the Podman bridge interface (commonly cni-podman0 or podman0)
PODMAN_IFACE="cni-podman0"

# Allow DNS
iptables -A FORWARD -i $PODMAN_IFACE -p udp --dport 53 -j ACCEPT
iptables -A FORWARD -i $PODMAN_IFACE -p tcp --dport 53 -j ACCEPT

# Allow HTTPS to specific LLM provider CIDRs
iptables -A FORWARD -i $PODMAN_IFACE -p tcp --dport 443 -d 13.107.238.0/24 -j ACCEPT  # Azure OpenAI
iptables -A FORWARD -i $PODMAN_IFACE -p tcp --dport 443 -d 35.199.224.0/19 -j ACCEPT  # Vertex AI

# Allow traffic to other containers on the same network (Temporal, workflow runner)
iptables -A FORWARD -i $PODMAN_IFACE -o $PODMAN_IFACE -j ACCEPT

# Drop all other forwarded traffic from Podman containers
iptables -A FORWARD -i $PODMAN_IFACE -j DROP
```

**nftables example**:

```bash
nft add table inet podman-egress
nft add chain inet podman-egress forward { type filter hook forward priority 0 \; }
nft add rule inet podman-egress forward iifname "cni-podman0" udp dport 53 accept
nft add rule inet podman-egress forward iifname "cni-podman0" tcp dport 53 accept
nft add rule inet podman-egress forward iifname "cni-podman0" tcp dport 443 ip daddr 13.107.238.0/24 accept  # Azure OpenAI
nft add rule inet podman-egress forward iifname "cni-podman0" tcp dport 443 ip daddr 35.199.224.0/19 accept  # Vertex AI
nft add rule inet podman-egress forward iifname "cni-podman0" oifname "cni-podman0" accept
nft add rule inet podman-egress forward iifname "cni-podman0" drop
```

Adjust the interface name and CIDRs for your environment. These rules apply to all containers on the Podman network — scope them further if running non-agent workloads on the same host.

---

## Demo

See [examples/DEMO.md](../examples/DEMO.md) for the interactive dashboard, demo recording, and terminal setup.

---

## API Reference

### Submit a workflow

```bash
curl -s -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{
    "definition": {
      "apiVersion": "v1",
      "kind": "AgentWorkflow",
      "metadata": { "name": "diagnose-production" },
      "spec": { "steps": [...] }
    },
    "provider": {
      "name": "openai",
      "model": "gpt-4o",
      "credentials_secret": "OPENAI_API_KEY"
    },
    "sandbox_image": "lightspeed-agentic-sandbox:latest"
  }'
# → {"workflow_id": "wf-abc123"}
```

Or register a definition first:

```bash
# Register
python3 -c "import yaml,json,sys; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))" \
  examples/workflow-definitions/diagnostic-workflow.yaml | \
  curl -s -X POST http://localhost:8080/v1/workflows/definitions \
    -H 'Content-Type: application/json' -d @-

# Trigger by name
curl -s -X POST http://localhost:8080/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{"workflow_name": "diagnose-production", "provider": {...}, "sandbox_image": "..."}'
```

### Check status

```bash
curl -s http://localhost:8080/v1/workflows/<workflow_id> | python3 -m json.tool
```

### Approve a step

```bash
curl -s -X POST http://localhost:8080/v1/workflows/<workflow_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"step_name": "approve-fix", "decision": "approved"}'
```

### Stream events (SSE)

```bash
curl -N http://localhost:8080/v1/workflows/<workflow_id>/events
```

### Cancel

```bash
curl -s -X POST http://localhost:8080/v1/workflows/<workflow_id>/cancel
```

---

## Workflow Definition Reference

### Step types

| Type | Purpose |
|------|---------|
| `agent` | Spawns a sandbox container running a complete agent loop (multi-turn LLM + tool calls) |
| `human-approval` | Pauses workflow, sends notification, waits for approval signal or timeout |

### Step fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique step identifier |
| `type` | Yes | `agent` or `human-approval` |
| `output_key` | Yes | Key for this step's result in workflow state |
| `prompt` | For agent | Prompt template (supports `{{ steps.X.output.Y }}` interpolation) |
| `output_schema` | No | JSON Schema for structured output (arrays require `items`) |
| `timeout_seconds` | No | Max seconds for this step (default: 600 for agent, 86400 for approval) |
| `condition` | No | Skip step if expression evaluates to false |
| `risk_level` | No | `low`, `medium`, `high`, `critical` — used by auto-approve policy |
| `message` | For approval | Human-readable approval request message |
| `max_retries` | No | Number of retry attempts (default: 1) |
| `parallel_group` | No | Steps sharing the same group run concurrently |
| `mcp_servers` | No | List of MCP server names (from run request catalog) to inject into this step |

### API request fields

The workflow YAML defines *what* (steps, prompts, schemas). The API request provides *how*:

| Field | Description |
|-------|-------------|
| `provider` | `{name, model, credentials_secret}` — LLM provider config |
| `sandbox_image` | Container image for agent steps |
| `skills_image` / `skills_paths` | Optional skills OCI image |
| `mcp_servers` | MCP server catalog — `[{name, url, headers}]`. Steps reference by name. |
| `approval_policy` | `{auto_approve_risk_levels: ["low"]}` |
| `workflow_id` | Optional caller-supplied idempotency key |

### Example definitions

See `examples/workflow-definitions/` for working workflow YAMLs:
- `diagnostic-workflow.yaml` — diagnose + approve
- `diagnose-fix-workflow.yaml` — diagnose → approve → fix → verify
- `mcp-filesystem-workflow.yaml` — gather context via MCP tools → recommend
- `security-audit-workflow.yaml` — audit → approve (critical) → remediate
- `ephemeral-diagnose-workflow.yaml` — single diagnostic step

These are validated by CI against the Pydantic schema.

---

## Cleanup

```bash
make down       # Podman (core)
make demo-down  # Podman (demo stack)
make kind-down  # Kubernetes
make clean      # stop + remove leftover sandbox containers
```
