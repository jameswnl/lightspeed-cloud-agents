# OpenShell Standalone Podman Gateway Setup

**Date**: 2026-07-08
**Issue**: [#74](https://github.com/jameswnl/lightspeed-cloud-agents/issues/74)
**Status**: Working end-to-end (with workaround)

## Overview

This documents how to run the OpenShell gateway as a standalone binary with the Podman driver, outside of the k3s deployment path. The goal is to get the full sandbox lifecycle working: create, wait READY, exec, delete.

## Prerequisites

- OpenShell gateway binary built from source: `~/ws/openshell/target/release/openshell-gateway`
- Podman running (macOS: `podman machine start`)
- A sandbox image with `iproute2` installed and a `sandbox` user (see below)

## Step 1: Generate Certs

```bash
/path/to/openshell-gateway generate-certs --output-dir /tmp/openshell-certs
```

This creates:
```
/tmp/openshell-certs/
  ca.crt, ca.key           # CA certificate and key
  server/tls.crt, tls.key  # Server TLS certificate
  client/tls.crt, tls.key  # Client TLS certificate (for mTLS)
  jwt/signing.pem           # Ed25519 signing key for sandbox JWTs
  jwt/public.pem            # Ed25519 public key
  jwt/kid                   # Key ID
```

## Step 2: Write TOML Config

```bash
cat > /tmp/openshell-gateway.toml << 'EOF'
[openshell.gateway.gateway_jwt]
signing_key_path = "/tmp/openshell-certs/jwt/signing.pem"
public_key_path = "/tmp/openshell-certs/jwt/public.pem"
kid_path = "/tmp/openshell-certs/jwt/kid"
gateway_id = "dev-gateway"
ttl_secs = 3600

[openshell.gateway.auth]
allow_unauthenticated_users = true
EOF
```

The `gateway_jwt` section enables sandbox JWT minting. The `auth.allow_unauthenticated_users` section allows client connections without mTLS or OIDC -- **only for local development**.

## Step 3: Build a Sandbox-Compatible Image

The supervisor requires:
1. `iproute2` installed (the `ip` command with `netns` support)
2. A `sandbox` user and group
3. CAP_NET_ADMIN and CAP_SYS_ADMIN (provided by the gateway's container config)

```dockerfile
FROM docker.io/library/fedora:40
RUN dnf install -y iproute && dnf clean all
RUN groupadd -r sandbox && useradd -r -g sandbox -m -d /home/sandbox sandbox
```

Build it:
```bash
podman build -t openshell-test-sandbox -f Containerfile .
```

Alpine does **not** work: BusyBox's `ip` command lacks `netns add` support.
Ubuntu minimal does **not** work: `iproute2` is not installed by default.

## Step 4: Start the Gateway

```bash
OPENSHELL_DRIVERS=podman \
OPENSHELL_LOG_LEVEL=info \
/path/to/openshell-gateway \
  --disable-tls \
  --config /tmp/openshell-gateway.toml \
  --bind-address 0.0.0.0 \
  --port 17670
```

Verify in logs:
- `Connected to Podman` with `rootless=true`
- `Bridge network ready` with a gateway IP
- `gateway-minted sandbox JWT enabled`
- `Unauthenticated user access enabled`
- `Server listening`

### Alternative: TLS Mode

For mTLS-authenticated connections:

```bash
OPENSHELL_DRIVERS=podman \
/path/to/openshell-gateway \
  --tls-cert /tmp/openshell-certs/server/tls.crt \
  --tls-key /tmp/openshell-certs/server/tls.key \
  --tls-client-ca /tmp/openshell-certs/ca.crt \
  --enable-mtls-auth true \
  --config /tmp/openshell-gateway.toml \
  --bind-address 127.0.0.1 \
  --port 17670
```

Python client with mTLS:
```python
from pathlib import Path
from openshell import SandboxClient, TlsConfig

client = SandboxClient(
    '127.0.0.1:17670',
    tls=TlsConfig(
        ca_path=Path('/tmp/openshell-certs/ca.crt'),
        cert_path=Path('/tmp/openshell-certs/client/tls.crt'),
        key_path=Path('/tmp/openshell-certs/client/tls.key'),
    ),
)
```

**Note**: mTLS mode causes a protocol mismatch with the supervisor. The Podman driver auto-detects the endpoint as `http://host.containers.internal:17670` regardless of TLS config, so the supervisor tries plaintext while the gateway expects TLS. Use `--disable-tls` with `allow_unauthenticated_users` for development.

## Step 5: Known Issue -- Podman Secret Mount

**Blocker**: The gateway mints a JWT at sandbox creation time and stores it in a Podman secret. The Podman driver's container spec includes a secret file mount at `/etc/openshell/auth/sandbox.jwt`, but the mount is **not applied** to the container on Podman 5.8.x. The secret exists (`podman secret ls`), the env var `OPENSHELL_SANDBOX_TOKEN_FILE` is set, but the file is missing inside the container.

### Workaround

After creating the sandbox, extract the token from the Podman secret and manually copy it into the container:

```python
import json, subprocess, tempfile, os

def inject_token_workaround(sandbox_id: str, sandbox_name: str) -> None:
    """Extract JWT from Podman secret and inject into container."""
    container_name = f"openshell-sandbox-{sandbox_name}"
    secret_name = f"openshell-token-{sandbox_id}"

    # Read the token from the Podman secret
    result = subprocess.run(
        ["podman", "secret", "inspect", "--showsecret", secret_name],
        capture_output=True, text=True, check=True,
    )
    token = json.loads(result.stdout)[0]["SecretData"].strip()

    # Stop container, copy token, restart
    subprocess.run(["podman", "stop", "-t", "0", container_name], capture_output=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jwt", delete=False) as f:
        f.write(token + "\n")
        tmp_path = f.name

    try:
        subprocess.run(
            ["podman", "cp", tmp_path,
             f"{container_name}:/etc/openshell/auth/sandbox.jwt"],
            capture_output=True, check=True,
        )
    finally:
        os.unlink(tmp_path)

    subprocess.run(["podman", "start", container_name], capture_output=True)
```

This causes a brief error phase (supervisor fails on first boot, succeeds on second). The gateway correctly transitions the sandbox back to READY when the supervisor reconnects.

### Root Cause

The OpenShell Podman driver serializes secret mounts as `{"source": "...", "target": "...", "uid": 0, "gid": 0, "mode": 256}` in the container create spec. Podman 5.8.x does not apply these. This is likely a Podman REST API compatibility issue -- the CLI `--secret name,target=/path` works fine.

## Step 6: End-to-End Lifecycle

```python
from openshell import SandboxClient
from openshell._proto import openshell_pb2 as pb2
import time

client = SandboxClient("127.0.0.1:17670")

# Create
spec = pb2.SandboxSpec(
    template=pb2.SandboxTemplate(
        image="localhost/openshell-test-sandbox:latest",
    )
)
ref = client.create(spec=spec)

# Inject token (workaround)
time.sleep(4)
inject_token_workaround(ref.id, ref.name)

# Wait for READY (poll because wait_ready fails fast on error phase)
for _ in range(30):
    sb = client.get(ref.name)
    if sb.phase == 2:
        break
    time.sleep(2)

# Exec
result = client.exec(ref.id, ["echo", "hello from openshell"])
assert result.exit_code == 0
assert result.stdout.strip() == "hello from openshell"

# Delete
client.delete(ref.name)
```

## Verified Behavior

| Operation | Status | Notes |
|---|---|---|
| Gateway start (Podman driver) | Working | Connects to Podman, creates bridge network |
| Gateway JWT minting | Working | Ed25519 token minted at CreateSandbox |
| CreateSandbox | Working | Container created with supervisor side-loaded |
| Supervisor auth (JWT) | Working | With token injection workaround |
| WaitReady | Working | Supervisor reaches READY after token inject |
| ExecSandbox | Working | Runs as `sandbox` user in isolated namespace |
| DeleteSandbox | Working | Container and Podman resources cleaned up |
| Client mTLS auth | Working | But causes supervisor protocol mismatch |
| Client unauthenticated | Working | `allow_unauthenticated_users = true` |
| Podman secret file mount | Broken | Workaround via `podman cp` |

## Sandbox Exec Isolation

The supervisor creates an isolated execution environment:
- Commands run as the `sandbox` user (not root)
- Container environment variables are **not** propagated to exec commands
- Network namespace isolation is applied (requires `iproute2`)
- Filesystem access governed by Landlock policy

## Open Issues

1. **Podman secret mount**: Needs upstream fix or OpenShell driver workaround. Options:
   - OpenShell driver: use `OPENSHELL_SANDBOX_TOKEN` env var instead of file mount
   - OpenShell driver: use Podman `secret_env` (env-var injection) instead of file mount
   - Podman API: investigate the correct JSON format for secret mounts

2. **Supervisor endpoint TLS mismatch**: When gateway runs with TLS, the Podman driver auto-detects `http://` endpoint for the supervisor. The supervisor can't connect because the gateway expects TLS. Options:
   - Configure the driver's gRPC endpoint override
   - Use `--disable-tls` for dev/test

3. **Image requirements**: Sandbox images must include `iproute2` and a `sandbox` user. Our production sandbox image (`lightspeed-agentic-sandbox`) needs to be verified against these requirements.
