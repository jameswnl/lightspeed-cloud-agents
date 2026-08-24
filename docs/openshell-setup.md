# OpenShell Gateway Setup for Cloud Agents

This guide covers setting up and using the OpenShell gateway with Cloud Agents. OpenShell provides hardened sandbox isolation (Landlock, seccomp, network namespaces) on top of the standard container runtime, replacing direct spawner access with a gateway-mediated architecture.

For architecture diagrams, see [architecture-with-openshell.md](architecture-with-openshell.md).

## How It Works

```
Workflow Runner                    OpenShell Gateway               Sandbox Container
     │                                   │                              │
     │─── gRPC: CreateSandbox ──────────▶│                              │
     │                                   │─── Podman/K8s: create ──────▶│
     │                                   │─── inject supervisor ───────▶│ (PID 1)
     │                                   │─── mint JWT token ──────────▶│
     │                                   │                              │
     │◀── SandboxRef (name, id) ─────────│                              │
     │                                   │                              │
     │─── gRPC: ExecSandbox ────────────▶│─── exec into container ─────▶│ uvicorn starts
     │                                   │                              │
     │─── gRPC: ExposeService ──────────▶│─── register virtual host ───▶│
     │                                   │                              │
     │─── HTTP: POST /v1/agent/run ─────▶│─── reverse proxy (Host hdr) ▶│ agent processes
     │                                   │                              │
     │─── gRPC: DeleteSandbox ──────────▶│─── destroy container ───────▶│ ✕
```

The `OpenShellSpawner` in Cloud Agents communicates with the gateway via gRPC. The gateway manages the container lifecycle, injects a supervisor binary as PID 1, and mints per-sandbox JWTs for supervisor-to-gateway authentication.

The sandbox container's CMD (from the image) is passed to the supervisor as `OPENSHELL_SANDBOX_COMMAND`. Since the spawner starts the HTTP server via `exec_stream`, the image CMD should be a keep-alive process (`sleep infinity`), not the application server.

## Prerequisites

- **Podman** (macOS: `podman machine start`) or **Kubernetes**
- **OpenShell gateway image**: `localhost/openshell-gateway:latest`
- **Sandbox image**: built from [jameswnl/lightspeed-agentic-sandbox @ lcs-main](https://github.com/jameswnl/lightspeed-agentic-sandbox/tree/lcs-main)
- **openshell SDK**: `pip install openshell>=0.0.111` (installed via `[openshell]` extra)

### Sandbox Image Requirements

The sandbox image must satisfy:

| Requirement | Why |
|---|---|
| `iproute2` installed | Supervisor uses `ip netns` for network namespace isolation |
| `sandbox` user exists | Supervisor drops privileges to this user |
| CMD is a keep-alive process | Spawner starts the app server via `exec_stream` |
| No `catatonit` / custom ENTRYPOINT | Supervisor replaces the entrypoint with itself |

The production Containerfile at `lightspeed-agentic-sandbox` already satisfies these. For local dev, use `Containerfile.dev`:

```bash
cd ~/ws/lightspeed-agentic-sandbox
podman build -f Containerfile.dev -t localhost/sandbox-openshell:dev .
```

## Gateway Setup

### Step 1: Generate JWT Signing Keys

The gateway mints per-sandbox JWTs so the supervisor can authenticate back. Generate the Ed25519 key pair:

```bash
podman run --rm --user 0 --security-opt label=disable \
  localhost/openshell-gateway:latest \
  sh -c 'openshell-gateway generate-certs --output-dir /tmp/certs 2>/dev/null && \
    echo "---SIGNING---" && cat /tmp/certs/jwt/signing.pem && \
    echo "---PUBLIC---" && cat /tmp/certs/jwt/public.pem && \
    echo "---KID---" && cat /tmp/certs/jwt/kid && echo ""'
```

Save the output to three files:

```
/path/to/jwt/
  signing.pem     # Ed25519 private key
  public.pem      # Ed25519 public key
  kid             # Key ID (hex string)
```

On macOS with Podman machine, copy the files into the VM:

```bash
mkdir -p /tmp/openshell-jwt
# ... write signing.pem, public.pem, kid to /tmp/openshell-jwt/
# Copy into VM (podman volumes mount from the VM, not macOS host)
cat /tmp/openshell-jwt/signing.pem | podman machine ssh -- "mkdir -p /tmp/openshell-jwt && cat > /tmp/openshell-jwt/signing.pem"
cat /tmp/openshell-jwt/public.pem  | podman machine ssh -- "cat > /tmp/openshell-jwt/public.pem"
cat /tmp/openshell-jwt/kid         | podman machine ssh -- "cat > /tmp/openshell-jwt/kid"
```

### Step 2: Write Gateway Config

Create a TOML config file:

```toml
# gateway.toml
[openshell.gateway.gateway_jwt]
signing_key_path = "/jwt/signing.pem"
public_key_path = "/jwt/public.pem"
kid_path = "/jwt/kid"

[openshell.gateway.auth]
allow_unauthenticated_users = true
```

> **Production**: Remove `allow_unauthenticated_users` and configure OIDC or mTLS instead. See [TLS mode](#tls-mode) below.

On macOS:

```bash
podman machine ssh -- "cat > /tmp/openshell-gateway.toml" << 'EOF'
[openshell.gateway.gateway_jwt]
signing_key_path = "/jwt/signing.pem"
public_key_path = "/jwt/public.pem"
kid_path = "/jwt/kid"

[openshell.gateway.auth]
allow_unauthenticated_users = true
EOF
```

### Step 3: Start the Gateway

#### macOS (Podman machine)

The Podman socket lives inside the VM. Mount it from the VM-internal path:

```bash
podman run -d --name openshell-gw \
  --security-opt label=disable \
  -p 17670:17670 \
  -v /run/user/501/podman/podman.sock:/run/podman/podman.sock \
  -v /tmp/openshell-jwt:/jwt:ro \
  -v /tmp/openshell-gateway.toml:/etc/openshell/gateway.toml:ro \
  -e OPENSHELL_PODMAN_SOCKET=/run/podman/podman.sock \
  localhost/openshell-gateway:latest \
  openshell-gateway --disable-tls --drivers podman --bind-address 0.0.0.0 \
  --config /etc/openshell/gateway.toml
```

> **Note**: The Podman socket UID (`/run/user/<UID>/podman/podman.sock`) varies. Find yours with:
> ```bash
> podman machine inspect 2>&1 | grep -A2 PodmanSocket
> ```

#### Linux (native Podman)

```bash
podman run -d --name openshell-gw \
  -p 17670:17670 \
  -v /run/user/$(id -u)/podman/podman.sock:/run/podman/podman.sock \
  -v /path/to/jwt:/jwt:ro \
  -v /path/to/gateway.toml:/etc/openshell/gateway.toml:ro \
  -e OPENSHELL_PODMAN_SOCKET=/run/podman/podman.sock \
  localhost/openshell-gateway:latest \
  openshell-gateway --disable-tls --drivers podman --bind-address 0.0.0.0 \
  --config /etc/openshell/gateway.toml
```

#### Native Binary (no container)

If you've built the gateway from source:

```bash
OPENSHELL_DRIVERS=podman \
OPENSHELL_LOG_LEVEL=info \
openshell-gateway \
  --disable-tls \
  --config /path/to/gateway.toml \
  --bind-address 0.0.0.0 \
  --port 17670
```

### Step 4: Verify

Check the gateway logs for these lines:

```
Connected to Podman                 cgroup_version=v2 rootless=true
Bridge network ready                network=openshell gateway_ip=Some("10.89.5.1")
gateway-minted sandbox JWT enabled  gateway_id=openshell ttl_secs=0
Server listening                    address=0.0.0.0:17670
```

Quick smoke test:

```python
from openshell import SandboxClient
from openshell._proto import openshell_pb2

client = SandboxClient(endpoint="localhost:17670")
spec = openshell_pb2.SandboxSpec(
    template=openshell_pb2.SandboxTemplate(image="localhost/sandbox-openshell:dev")
)
ref = client.create(workspace="default", spec=spec)
print(f"Created: {ref.name} ({ref.id})")
client.wait_ready(ref.name, workspace="default", timeout_seconds=60)
print("Ready!")
client.delete(ref.name, workspace="default")
print("Deleted.")
```

## Cloud Agents Configuration

### Workflow Runner

Set the following environment variables on the workflow runner container:

| Env Var | Default | Description |
|---|---|---|
| `WORKFLOW_SPAWNER` | *(empty)* | Set to `openshell` to enable OpenShellSpawner |
| `OPENSHELL_GATEWAY_URL` | `localhost:17670` | Gateway gRPC endpoint (no `http://` prefix) |
| `OPENSHELL_DRIVER` | `podman` | Compute driver: `podman` or `kubernetes` |
| `OPENSHELL_WORKSPACE` | `default` | OpenShell workspace name |

Example:

```bash
export WORKFLOW_SPAWNER=openshell
export OPENSHELL_GATEWAY_URL=localhost:17670
export OPENSHELL_DRIVER=podman
export OPENSHELL_WORKSPACE=default
```

### Workflow Definitions

Workflow steps with `spawn: ephemeral` (the default) will use the OpenShell spawner. No changes to workflow YAML are needed — the spawner is transparent to workflow authors.

```yaml
spec:
  steps:
    - name: diagnose
      type: agent
      spawn: ephemeral          # uses OpenShellSpawner when WORKFLOW_SPAWNER=openshell
      prompt: "Diagnose the issue..."
      output_key: diagnosis
```

Steps with `spawn: none` (in-process) or `spawn: local` (subprocess) bypass the spawner entirely and are unaffected.

### Skills Image (Podman driver)

When using the Podman driver, skills are mounted as native OCI image volumes (no extraction or streaming):

```json
{
  "skills_image": "quay.io/my-org/my-skills:latest",
  "skills_paths": ["/skills"]
}
```

The spawner configures Podman `image` mounts via `driver_config` on the `SandboxTemplate`. For the Kubernetes driver, skills are extracted locally and streamed into the sandbox via `tar` over `exec_stream`.

### Credentials

The spawner injects LLM credentials via the OpenShell Provider API:

1. Creates a Provider with the credential key-value pair
2. Attaches the Provider to the sandbox
3. Falls back to file injection (`/var/run/secrets/llm-credentials/`) if the Provider API fails

```yaml
provider:
  name: openai
  model: gpt-4o
  credentials_secret: OPENAI_API_KEY  # env var name containing the API key
```

### Network Policy

The `OpenShellSpawner` automatically derives network policy from the step configuration:

- **LLM provider egress**: Allows HTTPS to the provider host (e.g., `api.openai.com:443`)
- **Custom provider URL**: Allows egress to any `LIGHTSPEED_PROVIDER_URL`
- **MCP server egress**: Parses `LIGHTSPEED_MCP_SERVERS` JSON and allows egress to each server

No manual network policy YAML is required.

## End-to-End Test

Full lifecycle test (create → exec → health check → destroy):

```python
import asyncio
from openshell import SandboxClient
from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

async def test():
    client = SandboxClient(endpoint="localhost:17670")
    spawner = OpenShellSpawner(
        openshell_client=client,
        driver="podman",
        workspace="default",
    )

    endpoint = await spawner.spawn(
        agent_name="test-agent",
        image="localhost/sandbox-openshell:dev",
        env={"LIGHTSPEED_AGENT_PROVIDER": "openai"},
    )
    print(f"Endpoint: {endpoint}")

    import httpx
    headers = spawner.get_sandbox_headers("test-agent")
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(f"{endpoint}/health", headers=headers)
        print(f"Health: {resp.status_code} {resp.text}")

    await spawner.destroy("test-agent")
    print("Done.")

asyncio.run(test())
```

## TLS Mode

For production, configure mTLS on the gateway:

```bash
openshell-gateway generate-certs --output-dir /tmp/openshell-certs
```

Gateway config:

```toml
[openshell.gateway.tls]
cert_path = "/certs/server/tls.crt"
key_path = "/certs/server/tls.key"
client_ca_path = "/certs/ca.crt"

[openshell.gateway.mtls_auth]
enabled = true

[openshell.gateway.gateway_jwt]
signing_key_path = "/certs/jwt/signing.pem"
public_key_path = "/certs/jwt/public.pem"
kid_path = "/certs/jwt/kid"
```

Python client with mTLS:

```python
from pathlib import Path
from openshell import SandboxClient, TlsConfig

client = SandboxClient(
    "gateway.example.com:17670",
    tls=TlsConfig(
        ca_path=Path("/certs/ca.crt"),
        cert_path=Path("/certs/client/tls.crt"),
        key_path=Path("/certs/client/tls.key"),
    ),
)
```

> **Known limitation**: The Podman driver auto-detects the supervisor endpoint as `http://host.containers.internal:17670` regardless of TLS config. Use `--disable-tls` with `allow_unauthenticated_users` for Podman-based development.

## Troubleshooting

### Sandbox exits with code 1 immediately

**Symptom**: `SandboxError: sandbox X entered error phase` within seconds of creation.

**Check gateway logs**:

```bash
podman logs openshell-gw 2>&1 | grep -i 'error\|fail\|warn'
```

**Common causes**:

| Cause | Log message | Fix |
|---|---|---|
| Missing JWT config | `no sandbox token source available` | Add `[openshell.gateway.gateway_jwt]` to config |
| Image has custom ENTRYPOINT | `Container exited with code 1` | Remove ENTRYPOINT from image; OpenShell injects its own supervisor |
| Missing `iproute2` | `ip: command not found` | Install `iproute2` in the sandbox image |
| Missing `sandbox` user | `user sandbox not found` | Add `useradd sandbox` to Dockerfile |
| Wrong Podman socket path | `Podman socket not found` | Set `OPENSHELL_PODMAN_SOCKET` to the correct path |

### Sandbox gets stuck in Provisioning

**Symptom**: `wait_ready` hangs and eventually times out.

The supervisor may be retrying policy fetch. Check:

```bash
podman logs $(podman ps -a --filter "name=openshell-sandbox" --format "{{.Names}}" | head -1) 2>&1
```

### Health check passes but HTTP requests fail

**Symptom**: Sandbox shows READY but `POST /v1/agent/run` returns connection errors.

The spawner uses `ExposeService` to get a gateway-proxied endpoint with virtual-host routing. Requests must include the `Host` header:

```python
headers = spawner.get_sandbox_headers(agent_name)
# headers = {"Host": "sandbox-name.openshell.localhost"}
resp = await client.get(f"{endpoint}/health", headers=headers)
```

### Exec relay closed before exit status

**Symptom**: `UNAVAILABLE: exec relay closed before the command reported an exit status`

This is expected when the sandbox is destroyed while a background exec (like `start_server`) is still running. The spawner's `_do_destroy` cancels the server task before deleting the sandbox, but the cancellation races with the gRPC stream teardown.

## Gateway Environment Reference

| Env Var | Default | Description |
|---|---|---|
| `OPENSHELL_DRIVERS` | *(auto-detect)* | Compute driver: `podman`, `kubernetes`, `docker` |
| `OPENSHELL_PODMAN_SOCKET` | `/run/user/<uid>/podman/podman.sock` | Podman API socket path |
| `OPENSHELL_BIND_ADDRESS` | `127.0.0.1` | Bind address for gRPC/HTTP |
| `OPENSHELL_SERVER_PORT` | `17670` | gRPC/HTTP port |
| `OPENSHELL_LOG_LEVEL` | `info` | Log level: `trace`, `debug`, `info`, `warn`, `error` |
| `OPENSHELL_GATEWAY_CONFIG` | | Path to TOML config file |
| `OPENSHELL_DISABLE_TLS` | `false` | Disable TLS (dev only) |
| `OPENSHELL_DB_URL` | | PostgreSQL URL for durable sandbox state |
