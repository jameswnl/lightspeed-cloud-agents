# Spike: OpenShell Gateway Spawner

**Issue**: [#50](https://github.com/jameswnl/lightspeed-cloud-agents/issues/50)
**Status**: Complete (prototype + findings)
**Date**: 2026-07-07

## Summary

Built an `OpenShellSpawner` implementing the `AgentSpawner` ABC using the OpenShell Python SDK (`openshell>=0.0.70`). The prototype uses Option C from the issue: start our sandbox image via the OpenShell Gateway, expose its HTTP port via `ExposeService`, and preserve the `POST /v1/agent/run` contract.

## Architecture

```
Workflow Runner
    |
    |  gRPC (openshell SDK)
    v
OpenShell Gateway
    |
    |  Container runtime driver (Podman / K8s / Docker / MicroVM)
    v
Sandbox Container (lightspeed-agentic-sandbox)
    :8080  -->  ExposeService  -->  gateway-provided URL
```

**Key design decisions:**

1. **Service exposure**: Python SDK lacks an `ExposeService` wrapper. We use the raw gRPC stub: `client._stub.ExposeService(openshell_pb2.ExposeServiceRequest(...))`. Response has `.url` field.
2. **Sandbox naming**: `SandboxRef` has no labels. We name sandboxes `ca-agent-{agent_name}` and filter by prefix in `_do_list_active()`.
3. **Async bridging**: SDK is synchronous gRPC. We use `asyncio.to_thread()` for all SDK calls.
4. **Connection**: `OPENSHELL_GATEWAY_URL` env var (explicit) or `SandboxClient.from_active_cluster()` (auto-resolve).

## What We Gain

| Capability | Current (K8s + Podman) | With OpenShell |
|---|---|---|
| Spawner implementations | 2 (duplicated logic) | 1 unified |
| Compute backends | K8s or Podman | Docker, Podman, K8s, MicroVM |
| Sandbox isolation | Container securityContext | Landlock + seccomp + network namespace |
| Network policy | Manual NetworkPolicy YAML | Declarative YAML with L7 inspection |
| SSRF protection | None | Built-in internal IP blocking |
| Credentials | K8s Secrets as env vars | Gateway-managed providers |
| Debug access | kubectl exec | Built-in exec + SSH |

## TLS Analysis

OpenShell Gateway proxies all traffic through its gRPC connection. The traffic flow is:

```
Runner  --gRPC TLS-->  Gateway  --internal-->  Sandbox
```

The gateway terminates TLS at the gRPC boundary and manages the internal network. This means:

- **App-level TLS (T51) is redundant** when using OpenShell. The gateway provides the transport security.
- The spawner logs a warning when `tls_certs` is passed, suggesting `SANDBOX_TLS_MODE=mesh`.
- For deployments without OpenShell (direct K8s/Podman), T51 app-level TLS remains necessary.

## Unsupported Features

These features from the existing spawners are not yet supported:

| Feature | Status | Path Forward |
|---|---|---|
| `skills_image` (init container) | Logs warning | Use OpenShell `ExecSandbox` RPC to copy skills |
| `credential_secret_name` | Logs warning | Use OpenShell credential providers |
| `mcp_secret_mounts` | Logs warning | Env var injection or credential providers |
| `read_only` filesystem | Ignored | OpenShell Landlock provides better isolation |
| `service_account` | Ignored | OpenShell manages identity |
| Label-based filtering | Name prefix only | SDK `SandboxRef` has no label support |

## Risks

1. **Alpha software**: OpenShell self-describes as alpha, single-player mode. API may change.
2. **Gateway dependency**: Adds infrastructure to operate (gateway process + optional CRD controller for K8s).
3. **SDK gaps**: `ExposeService` has no Python wrapper -- we call the raw gRPC stub. `SandboxClient.list()` has no label filter.
4. **Sandbox CRD**: K8s driver requires `agents.x-k8s.io` CRD installed in the cluster.
5. **No GPU support yet**: OpenShell docs mention GPU passthrough as planned but not shipped.

## Latency Estimate

Based on OpenShell architecture (not measured in production):

| Operation | Estimated Overhead |
|---|---|
| `client.create()` | ~200ms (gRPC round-trip + container start) |
| `client.wait_ready()` | Depends on image pull; comparable to K8s pod |
| `ExposeService` | ~100ms (gRPC + proxy setup) |
| **Total spawn overhead** | ~300ms above raw container start |

This is within the 5s budget from the decision criteria. Actual measurement requires a running gateway.

## Integration Testing Findings (2026-07-08)

Attempted to run OpenShell gateway with Podman driver on macOS. Results:

### What worked
- **Gateway binary builds from source** (Rust, requires z3 dependency)
- **Podman driver connects**: gateway detects Podman socket, creates bridge network, identifies container runtime
- **gRPC API works**: `CreateSandbox` RPC succeeds, containers are created
- **Supervisor side-loading**: binary injected into containers via Podman image volumes

### What failed
- **`openshell gateway start` (CLI)**: Always bootstraps a k3s cluster in a container. k3s kubelet requires `/dev/kmsg` which Podman's libkrun VM on macOS doesn't expose. Fatal error: `open /dev/kmsg: operation not permitted`
- **Supervisor → gateway auth**: The supervisor inside the sandbox needs to authenticate back to the gateway via JWT (`IssueSandboxToken` gRPC). Without JWT configured, supervisor fails with "no sandbox token source available". With JWT configured, the supervisor still can't complete the token exchange in plaintext mode.
- **Client → gateway auth**: Even with `--disable-tls`, enabling JWT causes the gateway to require auth on all client gRPC calls (`missing authorization header`). The CLI handles this via stored mTLS certs from `~/.config/openshell/gateways/`, but manual gateway runs don't have this setup.

### Root cause
OpenShell's auth bootstrapping is tightly coupled to the k3s deployment path. The `openshell gateway start` command creates the k3s cluster, generates certs, stores them for the CLI, and configures JWT — all in one flow. Running the gateway binary directly bypasses this, and there's no documented standalone Podman quickstart.

### Impact on RHEL deployment
On RHEL production (not macOS), the situation is different:
- k3s runs natively on Linux (no VM, no `/dev/kmsg` issue)
- `openshell gateway start` should work out of the box
- Alternatively, systemd + certgen + manual JWT config would work

The blocker is **dev/test on macOS with Podman**, not production RHEL deployment.

### Revised assessment
The original "Conditional Go" stands for RHEL/Linux targets. For macOS/Podman dev environments, OpenShell requires either Docker Desktop or a working standalone auth setup (issue #TBD).

## Standalone Gateway E2E Results (2026-07-08, issue #74)

Full end-to-end sandbox lifecycle verified on macOS with Podman driver. See [openshell-standalone-setup.md](openshell-standalone-setup.md) for complete setup instructions.

### Configuration that works

```
openshell-gateway --disable-tls --config gateway.toml
```

TOML config enables sandbox JWT minting (`[openshell.gateway.gateway_jwt]`) and unauthenticated client access (`[openshell.gateway.auth] allow_unauthenticated_users = true`).

### Verified operations

| Operation | Result |
|---|---|
| CreateSandbox | Working |
| Supervisor JWT auth | Working (with workaround) |
| WaitReady (phase=2) | Working |
| ExecSandbox | Working (runs as `sandbox` user) |
| DeleteSandbox | Working |
| Client mTLS auth | Working (but breaks supervisor) |

### Blockers identified and worked around

1. **Podman secret file mount broken** ([issue #82](https://github.com/jameswnl/lightspeed-cloud-agents/issues/82)): The OpenShell Podman driver creates a Podman secret containing the sandbox JWT and specifies a file mount at `/etc/openshell/auth/sandbox.jwt`. On Podman 5.8.x, the mount is not applied to the container (the secret exists in `podman secret ls` but the `secrets` field in container inspect is `[]`).
   - **Root cause**: The driver serializes the `secrets` field as `[{"source": "...", "target": "...", "uid": 0, "gid": 0, "mode": 256}]` (see `container.rs` `SecretMount` struct, line ~289). Podman 5.8.x does not apply this format via the REST API, though the CLI `--secret name,target=/path` works fine. This is a Podman REST API compatibility issue.
   - **Env var injection not viable**: The driver explicitly strips `OPENSHELL_SANDBOX_TOKEN` from the container env (`container.rs` line 432) as a security measure. Even if we set it in the SandboxSpec environment, the driver removes it. The `secret_env` field exists in the `ContainerSpec` struct but is always empty.
   - **Supervisor token acquisition order** (`grpc_client.rs`): The supervisor reads the JWT from (1) `OPENSHELL_SANDBOX_TOKEN` env var (test harness path), (2) `OPENSHELL_SANDBOX_TOKEN_FILE` file path (production path), (3) K8s ServiceAccount token exchange.
   - **Automated workaround implemented**: `OpenShellSpawner._inject_podman_token()` — after `create_sandbox()`, extracts the JWT from the Podman secret via CLI, copies it into the stopped container, and restarts. Activated by passing `podman_cli="/usr/bin/podman"` to the spawner constructor. No-op on K8s path (where `podman_cli` is None).
   - **Upstream fix still needed**: OpenShell driver should use `secret_env` (env-var-based Podman secret injection) or fix the `secrets` JSON format for Podman 5.8.x compatibility.

2. **Sandbox images need iproute2 + sandbox user**: The supervisor creates network namespaces via `ip netns add` and drops privileges to a `sandbox` user. Alpine and Ubuntu minimal images fail. Fedora 40 + `dnf install iproute` + `useradd sandbox` works.

3. **TLS endpoint mismatch**: The Podman driver auto-detects `http://host.containers.internal:17670` as the supervisor-to-gateway endpoint regardless of gateway TLS config. Running the gateway with TLS causes the supervisor to fail (plaintext to TLS gateway). Use `--disable-tls` for dev/test. On production RHEL with k3s, this is handled by the deployment pipeline.

### Sandbox isolation behavior

The supervisor enforces isolation for exec'd commands:
- User: `sandbox` (not root)
- Container env vars are **not** propagated (security measure)
- Network namespace isolation via Landlock + seccomp
- `/sandbox` workspace directory is writable

### Auth architecture (from source analysis)

```
Client --[mTLS or OIDC]--> Gateway --[JWT in Podman Secret]--> Supervisor
                                                                    |
                                                         reads OPENSHELL_SANDBOX_TOKEN_FILE
                                                         authenticates via Bearer JWT
                                                         on all gRPC calls back to gateway
```

- Gateway mints Ed25519 JWT (EdDSA) at CreateSandbox time
- JWT contains SPIFFE-format subject: `spiffe://openshell/sandbox/<sandbox_id>`
- Supervisor reads token from file, sends as Bearer header
- Gateway validates kid, signature, audience (`openshell-gateway:<gateway_id>`), expiry
- Supervisor refreshes token at 80% TTL via `RefreshSandboxToken` RPC
- K8s path uses ServiceAccount projected token + `IssueSandboxToken` exchange (different from Podman path)

## RHEL Verification (2026-07-09, issues #81, #92)

End-to-end testing on AWS EC2 RHEL 9.6 instance (m5.large, kernel 5.14.0, Podman 5.4.0).

### Environment

- **Host**: RHEL 9.6, kernel 5.14.0-570.123.1.el9_6.x86_64
- **Podman**: 5.4.0 (rootless, cgroup v2)
- **OpenShell**: v0.0.80 (pre-built gateway binary + source-built supervisor)
- **LSMs**: lockdown, capability, landlock, yama, selinux, bpf

### Setup Prerequisites

1. **Cgroup CPU delegation** — rootless Podman on RHEL 9 does not delegate the CPU controller by default. OpenShell sets CPU limits on sandboxes, which fails without it.
   ```
   sudo mkdir -p /etc/systemd/system/user@.service.d
   cat > delegate.conf <<EOF
   [Service]
   Delegate=cpu cpuset io memory pids
   EOF
   sudo systemctl daemon-reload
   ```
2. **Podman socket** — `systemctl --user enable --now podman.socket`
3. **Gateway bind address** — must use `--bind-address 0.0.0.0` (not default `127.0.0.1`) so containers can reach the gateway via bridge network
4. **Gateway JWT config** — v0.0.80 changed from `secret` to file-based Ed25519 keys (`signing_key_path`, `public_key_path`, `kid_path`)
5. **Sandbox image requirements** — must include `iproute2` (for `ip`), `util-linux-core` (for `nsenter`), and a `sandbox` user

### Gateway Configuration (v0.0.80)

```toml
[openshell.gateway]
supervisor_image = "localhost/openshell-supervisor:latest"
compute_drivers = ["podman"]

[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.gateway.gateway_jwt]
signing_key_path = "keys/signing.pem"
public_key_path = "keys/public.pem"
kid_path = "keys/kid"
gateway_id = "rhel-test"
```

### Results by Image

| Image | Base | glibc | Pre-built supervisor (v0.0.80) | Source-built supervisor | Sandbox Status |
|---|---|---|---|---|---|
| Fedora 40 | Fedora | 2.39 | **Works** | N/A | **Ready** (healthy) |
| UBI 10 (RHEL 10.2) | RHEL 10 | 2.39 | **Works** | N/A | **Ready** (healthy) |
| UBI 9 (RHEL 9.8) | RHEL 9 | 2.34 | **FAILS** (`GLIBC_2.38` / `GLIBC_2.39` not found) | **Works** (14 min build) | **Ready** (healthy) |

### Isolation Verification (all three images)

| Check | Result |
|---|---|
| Exec runs as `sandbox` user | `uid=1000(sandbox) gid=1000(sandbox)` |
| Seccomp enforced | `Seccomp: 2`, `Seccomp_filters: 3`, `NoNewPrivs: 1` |
| Network namespace isolated | Separate veth `10.200.0.2/24`, not host network |
| CreateSandbox latency | ~300ms |
| DeleteSandbox | Clean (container + volume removed) |

### RHEL 9 glibc Compatibility Issue

The pre-built OpenShell v0.0.80 supervisor binary (`openshell-sandbox`) is compiled against glibc 2.39 (Fedora 40 toolchain). It does not run on RHEL 9 (glibc 2.34) — neither on the host nor inside UBI 9 containers.

**Workaround**: Build the supervisor from source on RHEL 9. Requires Rust toolchain + build dependencies (`gcc`, `gcc-c++`, `cmake`, `openssl-devel`, `clang-devel`). Build time: ~14 minutes on m5.large.

```bash
git clone --depth 1 https://github.com/nvidia/openshell.git
cd openshell
cargo build --release --bin openshell-sandbox
```

The resulting binary links against glibc 2.34 and works in UBI 9 containers.

**Upstream fix needed**: OpenShell should either ship a musl-static supervisor binary or provide builds targeting RHEL 9/glibc 2.34. This is a build/packaging issue, not a runtime limitation — the supervisor code itself is compatible.

**RHEL 10 / UBI 10**: No issue. glibc 2.39 matches the pre-built binary.

### Containerized Gateway (DooD Pattern) — Verified

Tested containerized gateway on RHEL 9.6 with Podman 5.8.2:

```bash
podman run -d --name openshell-gateway \
  --network podman_default \
  -v /run/user/1000/podman/podman.sock:/run/podman/podman.sock \
  -v ~/openshell-config:/config:ro \
  -e OPENSHELL_PODMAN_SOCKET=/run/podman/podman.sock \
  -p 17670:17670 \
  --privileged \
  localhost/openshell-gateway:latest \
  --disable-tls --bind-address 0.0.0.0 --config /config/gateway.toml
```

**Result**: Gateway connects to host Podman via socket mount, creates and manages sandboxes. Sandbox lifecycle (create, exec, delete) verified through containerized gateway. Seccomp and network namespace isolation confirmed.

**Key requirements**:
- `OPENSHELL_PODMAN_SOCKET=/run/podman/podman.sock` — gateway runs as root in container, default socket path differs
- `--privileged` — required for Podman socket access
- Gateway container must be on the same network as the workflow runner for DNS resolution

### OpenShellSpawner SDK Migration Required

The `OpenShellSpawner` was written against an earlier OpenShell SDK version. The v0.0.78 SDK has breaking API changes:

| Old API (spawner code) | New API (v0.0.78) |
|---|---|
| `create_sandbox(image=, env=, labels=)` | `create(spec=SandboxSpec)` |
| `delete_sandbox(sandbox_id)` | `delete(sandbox_name)` |
| `expose_service(sandbox_id, port=)` | Not available — use port forwarding |
| `exec_stream(sandbox_id, command)` (async iterator) | `exec_stream(sandbox_id, command)` (sync iterator) |
| All methods async | All methods sync (need `asyncio.to_thread()`) |

The entrypoint wiring works (`WORKFLOW_SPAWNER=openshell` → creates `SandboxClient` → passes to `OpenShellSpawner`), but the spawner's internal calls fail at runtime. Tracked as a follow-up issue.

## Go/No-Go Recommendation

**Go** — RHEL verification confirms OpenShell works on both RHEL 9 and RHEL 10 with Podman driver. Seccomp, network namespace isolation, and sandbox lifecycle all verified.

### Resolved

1. **RESOLVED**: Standalone Podman gateway works on macOS with JWT config. Podman secret file mount is broken in 5.8.x but automated workaround implemented (issue #82).
2. **RESOLVED**: Supervisor auth chain works end-to-end with Podman driver (JWT minting, supervisor authentication).
3. **RESOLVED**: Podman secret file mount workaround implemented in `openshell_spawner.py`.
4. **RESOLVED**: RHEL 9 verification — gateway + supervisor + sandbox lifecycle works. Seccomp and network namespace isolation confirmed.
5. **RESOLVED**: UBI 9 sandbox images — requires source-built supervisor (glibc 2.34 compatibility). UBI 10 works with pre-built binaries.

### Remaining

1. OpenShell team commits to stabilizing the Python SDK (especially `ExposeService` wrapper)
2. Gateway resource overhead measurement in target deployment environments
3. L7 network policy testing with our sandbox image
4. Sandbox image must include `iproute2`, `nsenter` (`util-linux-core`), and a `sandbox` user
5. Containerized gateway deployment (DooD pattern) — not yet tested
6. Upstream: request musl-static supervisor binary or RHEL 9-compatible builds

**Immediate value**: Eliminates duplicated spawner code and provides defense-in-depth isolation (seccomp + network namespace). Even without L7 policy, this is a security improvement over container securityContext alone.

**If no-go**: The prototype code remains as a third spawner option. No changes to existing K8s/Podman spawners.

## Files

| File | Description |
|---|---|
| `src/cloud_agents/spawner/openshell_spawner.py` | Spawner implementation |
| `tests/unit/spawner/test_openshell_spawner.py` | 27 unit tests |
| `docs/spikes/openshell-standalone-setup.md` | Standalone gateway setup guide |
| `pyproject.toml` | `openshell` optional dependency |
| `docs/gaps/gaps-implementation-plan.md` | T53 entry |
