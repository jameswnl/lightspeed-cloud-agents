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

## Go/No-Go Recommendation

**Conditional Go** -- proceed with production hardening if the following are confirmed:

1. OpenShell team commits to stabilizing the Python SDK (especially `ExposeService` wrapper)
2. Gateway resource overhead is acceptable in target deployment environments
3. L7 network policy works with our sandbox image (needs integration test)

**Immediate value**: Eliminates duplicated spawner code and provides defense-in-depth isolation. Even without L7 policy, the Landlock + seccomp sandbox is a security improvement.

**If no-go**: The prototype code remains as a third spawner option. No changes to existing K8s/Podman spawners.

## Files

| File | Description |
|---|---|
| `src/cloud_agents/spawner/openshell_spawner.py` | Spawner implementation |
| `tests/unit/spawner/test_openshell_spawner.py` | 27 unit tests |
| `pyproject.toml` | `openshell` optional dependency |
| `docs/gaps/gaps-implementation-plan.md` | T53 entry |
