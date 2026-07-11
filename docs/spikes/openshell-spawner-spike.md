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

### OpenShellSpawner SDK Migration (Completed)

The `OpenShellSpawner` has been migrated to the v0.0.78+ SDK API (PRs #97–#100):

| Old API | New API (v0.0.78+) | Status |
|---|---|---|
| `create_sandbox(image=, env=, labels=)` | `create(spec=SandboxSpec)` | ✅ Done |
| `delete_sandbox(sandbox_id)` | `delete(sandbox_name)` | ✅ Done |
| `expose_service(sandbox_id, port=)` | Raw gRPC `ExposeService` via standalone channel | ✅ Done |
| `exec_stream(sandbox_id, command)` (async) | `exec_stream(sandbox_id, command)` (sync, wrapped in `asyncio.to_thread()`) | ✅ Done |

Additional changes:
- **Parallel-safe readiness**: `_wait_ready_with_host()` takes virtual host as parameter (no shared state race)
- **Auto-derived network policy**: `_build_network_policy()` derives L7 egress rules from provider name and MCP server config
- **Sandbox identity**: `get_sandbox_id()` returns UUID (for `exec_stream`), distinct from sandbox name (for `create`/`delete`/`wait_ready`)

## Go/No-Go Recommendation

**Go** — RHEL verification confirms OpenShell works on both RHEL 9 and RHEL 10 with Podman driver. Seccomp, network namespace isolation, and sandbox lifecycle all verified.

### Resolved

1. **RESOLVED**: Standalone Podman gateway works on macOS with JWT config.
2. **RESOLVED**: Supervisor auth chain works end-to-end with Podman driver (JWT minting, supervisor authentication).
3. **RESOLVED**: Podman 5.8.x JWT workaround — **removed**. OpenShell v0.0.79+ (PR NVIDIA/OpenShell#2156) delivers sandbox JWTs natively via Podman secrets when `gateway_jwt` is configured with signing keys. No client-side workaround needed. Requires gateway v0.0.79+.
4. **RESOLVED**: RHEL 9 verification — gateway + supervisor + sandbox lifecycle works. Seccomp and network namespace isolation confirmed.
5. **RESOLVED**: UBI 9 sandbox images — requires source-built supervisor (glibc 2.34 compatibility). UBI 10 works with pre-built binaries.
6. **RESOLVED**: Production gaps closed (PR #105) — skills image, credentials, filesystem policy, MCP secrets, TLS skip, service account skip, provider cleanup. All 7 example workflows pass E2E through OpenShell on RHEL 9 + Kind cluster.
7. **RESOLVED**: ~~Containerized gateway deployment (DooD pattern)~~ — verified (see "Containerized Gateway" section).

### Remaining

1. OpenShell team commits to stabilizing the Python SDK (especially `ExposeService` wrapper)
2. Gateway resource overhead measurement in target deployment environments
3. L7 network policy testing with live gateway (auto-derivation implemented in `_build_network_policy()`)
4. Sandbox image must include `iproute2`, `nsenter` (`util-linux-core`), and a `sandbox` user
5. Upstream: request musl-static supervisor binary or RHEL 9-compatible builds
6. Skills image extraction for OpenShell + K8s driver (issue #106, see Fragility Analysis below)

**Immediate value**: Eliminates duplicated spawner code and provides defense-in-depth isolation (seccomp + network namespace). Even without L7 policy, this is a security improvement over container securityContext alone.

**If no-go**: The prototype code remains as a third spawner option. No changes to existing K8s/Podman spawners.

## Fragility Analysis

Assessment of each production feature's robustness, what we've done, and known gaps.

### Skills image — moderate fragility

**What we've done**: `_load_skills()` extracts skills from an OCI image locally (Podman SDK or CLI), creates a tar, streams it into the sandbox via `exec_stream`. Works for OpenShell + Podman driver.

**Fragility**:
- **OpenShell + K8s driver**: Runner is a K8s pod with no Podman socket. Neither Podman SDK nor CLI is available. `_extract_skills_image()` raises `RuntimeError`. Issue #106 tracks this.
- **Full image pull**: `crane export` or Podman SDK downloads ALL layers of the skills image, not just the `/skills` directory. A fat base image (e.g. `python:3.12` + 5MB of skills) wastes bandwidth.

**Mitigation**: Build skills images as minimal single-layer images (`FROM scratch` + `COPY skills/ /skills/`). This is a convention, not a code change. A 5MB skills directory = 5MB image. The extraction code works efficiently with minimal images.

**Future options for K8s driver**:
1. Install `crane` in the sandbox image and pull from inside (no runner-side extraction)
2. OpenShell `driver_config` volume mounts (needs upstream investigation)
3. Operator pre-populates skills into sandbox spec

### Credentials (Provider API) — moderate fragility

**What we've done**: `_inject_credentials()` first tries the OpenShell Provider API (`CreateProvider` + `AttachSandboxProvider` via raw gRPC), then falls back to file injection via `_do_write_file()`.

**Fragility**:
- **Raw gRPC**: Uses `grpc.insecure_channel` and accesses `self._client._endpoint` (private attribute). If the OpenShell SDK changes its internal structure, this breaks silently.
- **Proto compatibility**: `CreateProviderRequest` and `AttachSandboxProviderRequest` protobuf messages must match the gateway version. No version negotiation.

**Mitigation**: File-based fallback catches Provider API failures and writes credentials to `/var/run/secrets/llm-credentials/`. This degraded path works for all providers. Missing credentials now raise `RuntimeError` (fail-closed) instead of silently skipping.

### MCP secret injection — solid

**What we've done**: `_inject_mcp_secrets()` creates directories via `exec_stream(mkdir -p)` and writes secret values via `_do_write_file()` (base64 + exec).

**Why it's solid**: Uses the same proven base64+exec pattern that `_do_write_file()` and `_do_read_file()` use for transcript collection. No external dependencies. Works on any OpenShell driver.

### Filesystem policy — solid

**What we've done**: `_build_filesystem_policy()` sets `spec.policy.filesystem.read_only = ["/"]` with explicit `read_write` exceptions for `/tmp`, `/home/agent`, `/var/log`, `/app/skills`, `/var/secrets/mcp`, `/var/run/secrets/llm-credentials`.

**Why it's solid**: Sets protobuf fields directly on `SandboxSpec.policy` — same mechanism as `_build_network_policy()`. Clean contract with no external dependencies.

### Network policy auto-derivation — solid

**What we've done**: `_build_network_policy()` derives deny-by-default L7 egress rules from `LIGHTSPEED_PROVIDER` (provider-to-host mapping) and `LIGHTSPEED_MCP_SERVERS` (parsed URLs). Uses scheme-based default ports (80 for http, 443 for https).

**Why it's solid**: Same protobuf contract as filesystem policy. Provider host mapping is a static dict. MCP URLs are parsed with stdlib `urlparse`.

### TLS / Service account — solid (not applicable)

**What we've done**: Log info and skip. The gateway provides transport security (TLS) and manages identity (no K8s SA equivalent). These are architectural decisions, not workarounds.

### Provider cleanup — solid

**What we've done**: `_do_destroy()` detaches providers via `DetachSandboxProvider` gRPC before deleting the sandbox. `_cleanup_sandbox()` does the same on spawn failure. Provider IDs are tracked in `self._provider_ids`.

**Minor fragility**: Uses the same raw gRPC pattern as credential injection. If detach fails, it logs a warning and continues with sandbox deletion (non-blocking).

## Spawner Comparison: OpenShell vs K8s vs Podman

### Isolation & Security

| | Podman | Kubernetes | OpenShell |
|---|---|---|---|
| **Isolation** | Linux containers (namespaces/cgroups) | K8s pods with hardened security context | MicroVM (strongest) |
| **Network egress** | Podman network (manual config) | Cluster NetworkPolicy (manual) | Auto-derived deny-by-default from provider + MCP config |
| **Filesystem** | Optional read-only flag | Always read-only rootfs | Granular read/write path policy |
| **TLS** | App-level cert injection | App-level cert injection | Not needed — gateway handles transport |

### Feature Parity

| Feature | Podman | Kubernetes | OpenShell |
|---|---|---|---|
| **Skills image** | Volume + transient container | Init container + emptyDir | Podman SDK extract + tar stream (Podman driver ✅, K8s driver ❌ issue #106) |
| **Credentials** | Skipped (warning) | Secret volume + envFrom | Provider API + file fallback |
| **MCP secrets** | Rejected (ValueError) | Secret volumes | File injection via exec |
| **Read-only mode** | Container flag | Always enforced | Policy with write exceptions |
| **Service account** | Ignored | PodSpec SA + projected token | N/A (gateway manages identity) |
| **Resource limits** | No | CPU/memory via SpawnConfig | No |
| **Progress streaming** | No | No | Yes (real-time JSONL tail) |

### Architecture

| | Podman | Kubernetes | OpenShell |
|---|---|---|---|
| **Primitive** | Container | Job + Service | Sandbox (microVM) |
| **Readiness** | Base class HTTP poll | Base class HTTP poll | Custom Host-header poll (gateway-routed) |
| **Routing** | `localhost:{random_port}` | `{name}.{ns}.svc:8080` | Gateway virtual-host (`Host` header) |
| **Cleanup** | Stop + remove container | Delete Job + Service | gRPC Delete + provider detach |
| **Instance tracking** | Podman label query | K8s Job label query | In-memory dicts |
| **Dependencies** | Podman socket | K8s API server | OpenShell gateway (gRPC) v0.0.79+ |

### Key Findings

1. **OpenShell is the most secure** — microVM isolation + auto-derived L7 network policy + gateway-managed credentials. K8s is second (always-hardened securityContext). Podman is weakest (socket access = host-level control).

2. **OpenShell is the most feature-complete** — only spawner with progress streaming, Provider API, auto network policy, and granular filesystem policy. K8s has resource limits that OpenShell lacks.

3. **Podman has the most gaps** — no credential injection, no MCP secrets (ValueError), no resource limits, no progress streaming. It is a dev/demo spawner.

4. **OpenShell eliminates TLS complexity** — gateway handles transport security, removing cert generation/mounting/cleanup code.

5. **Trade-off**: OpenShell adds a gateway infrastructure dependency. K8s/Podman spawners talk directly to the container runtime.

## Files

| File | Description |
|---|---|
| `src/cloud_agents/spawner/openshell_spawner.py` | Spawner implementation (52 tests) |
| `tests/unit/spawner/test_openshell_spawner.py` | Unit tests |
| `docs/spikes/openshell-standalone-setup.md` | Standalone gateway setup guide |
| `pyproject.toml` | `openshell` optional dependency |
| `docs/gaps/gaps-implementation-plan.md` | T53 entry |
