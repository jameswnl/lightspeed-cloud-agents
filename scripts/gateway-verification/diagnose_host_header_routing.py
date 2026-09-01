#!/usr/bin/env python3
"""Diagnose issue #249 (bare-404 Host-header routing) against a real gateway.

`OpenShellSpawner._wait_ready_with_host()` proxies `/health` through the
gateway's main TLS port using `ExposeService`'s returned virtual host as a
`Host` header. Issue #209/#249 describe a gateway ingress that returns a
bare `404` (no body, no `content-type` -- rejected before reaching FastAPI)
for this regardless of whether the `Host` header is correct. This script
isolates that one question from the rest of the spawn pipeline: it creates
a sandbox, starts the real app server, confirms the app itself is healthy
via a direct in-sandbox `curl 127.0.0.1:8080/health` (ruling out an app-level
failure), then issues three gateway-proxied requests -- correct `Host`, an
obviously-wrong `Host`, and no `Host` header at all -- and prints how each
one is routed.

Also surfaces `SandboxStatus.conditions` while polling for readiness
(`reason`/`message`/`last_transition_time`, Kubernetes-condition-shaped).
The gateway populates this with real diagnostic detail (e.g. `reason=
DependenciesNotReady, message="Pod exists with phase: Pending"` while a pod
is scheduling/pulling its image), but `openshell.sandbox.SandboxStatusRef`
(the SDK's `_sandbox_ref()` conversion, confirmed still true as of SDK
0.0.111 and the unreleased `~/ws/Openshell` source) silently drops
`conditions`, exposing only `phase`/`current_policy_version`/`exit_code`.
This script bypasses that by calling `GetSandbox` on the raw gRPC stub
directly, so a stuck-in-Provisioning sandbox (issue #248) shows its actual
condition reason instead of nothing -- no cluster-admin access needed.

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway
  SANDBOX_IMAGE     image to diagnose (must run lightspeed_agentic.app)

Optional env vars:
  GATEWAY_WORKSPACE            default: "default"
  GATEWAY_TLS_CA                path to a CA bundle; omit for plaintext gateways
  GATEWAY_BEARER_TOKEN          static bearer/OIDC token
  GATEWAY_OIDC_ISSUER            OIDC issuer base URL -- if set (with
                                 client id/secret below), mints a fresh
                                 token via the client-credentials grant
                                 instead of using a static
                                 GATEWAY_BEARER_TOKEN. Preferred for
                                 short-TTL OIDC gateways (e.g. hosted
                                 staging) over pre-extracting a token.
  GATEWAY_OIDC_CLIENT_ID
  GATEWAY_OIDC_CLIENT_SECRET
  GATEWAY_OIDC_AUDIENCE
  HEALTH_PATH                   default: "/health"
  GATEWAY_VERIFY_SANDBOX_NAME   default: "diagnose-host-header-routing"

Usage (hosted staging, OIDC client-credentials):
  GATEWAY_URL=<your-hosted-gateway-host>:443 \\
  GATEWAY_TLS_CA=$(uv run python3 -c 'import certifi; print(certifi.where())') \\
  GATEWAY_OIDC_ISSUER=https://keycloak.../realms/ambient-code \\
  GATEWAY_OIDC_CLIENT_ID=... GATEWAY_OIDC_CLIENT_SECRET=... GATEWAY_OIDC_AUDIENCE=... \\
  SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest \\
    uv run python scripts/gateway-verification/diagnose_host_header_routing.py

Usage (local-infra, plaintext):
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/diagnose_host_header_routing.py
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from cloud_agents.spawner.factory import build_spawner

GATEWAY_URL = os.environ["GATEWAY_URL"]
SANDBOX_IMAGE = os.environ["SANDBOX_IMAGE"]
WORKSPACE = os.environ.get("GATEWAY_WORKSPACE", "default")
TLS_CA = os.environ.get("GATEWAY_TLS_CA") or None
BEARER_TOKEN = os.environ.get("GATEWAY_BEARER_TOKEN") or None
HEALTH_PATH = os.environ.get("HEALTH_PATH", "/health")
SANDBOX_NAME = os.environ.get("GATEWAY_VERIFY_SANDBOX_NAME", "diagnose-host-header-routing")

OIDC_ISSUER = os.environ.get("GATEWAY_OIDC_ISSUER") or None
OIDC_CLIENT_ID = os.environ.get("GATEWAY_OIDC_CLIENT_ID") or None
OIDC_CLIENT_SECRET = os.environ.get("GATEWAY_OIDC_CLIENT_SECRET") or None
OIDC_AUDIENCE = os.environ.get("GATEWAY_OIDC_AUDIENCE") or None

# Mirrors OpenShellSpawner._DEFAULT_EXTRA_ENV -- not inherited by a bare
# `sandbox exec`, only by _do_spawn()'s own start_server() call.
SERVER_ENV = {
    "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
    "LIGHTSPEED_SKILLS_DIR": "/app/skills",
    "LIGHTSPEED_PROVIDER": "openai",
    "LIGHTSPEED_MODEL": "gpt-4o-mini",
    "OPENAI_API_KEY": "unused",
}
SERVER_CMD = [
    "python3",
    "-m",
    "uvicorn",
    "lightspeed_agentic.app:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8080",
]


def _fetch_oidc_token() -> str:
    """Mint a single access token via the OIDC client-credentials grant."""
    token_endpoint = OIDC_ISSUER.rstrip("/") + "/protocol/openid-connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": OIDC_CLIENT_ID,
        "client_secret": OIDC_CLIENT_SECRET,
    }
    if OIDC_AUDIENCE:
        data["audience"] = OIDC_AUDIENCE
    response = httpx.post(token_endpoint, data=data, timeout=10.0)
    response.raise_for_status()
    token = response.json()["access_token"]
    return str(token)


async def main() -> None:
    kwargs = dict(
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )
    if OIDC_ISSUER:
        token = await asyncio.to_thread(_fetch_oidc_token)
        kwargs["bearer_token_provider"] = lambda: token
    spawner = build_spawner("openshell", **kwargs)

    from openshell._proto import openshell_pb2

    spec = openshell_pb2.SandboxSpec(
        template=openshell_pb2.SandboxTemplate(image=SANDBOX_IMAGE, labels={})
    )
    for k, v in SERVER_ENV.items():
        if k != "OPENAI_API_KEY":
            spec.environment[k] = v
    spawner._build_network_policy(spec, SERVER_ENV)
    spawner._build_baseline_filesystem_policy(spec, allowed_skills=None)

    sandbox_ref = await asyncio.to_thread(spawner._client.create, workspace=WORKSPACE, spec=spec)
    sandbox_name, sandbox_id = sandbox_ref.name, sandbox_ref.id
    print(f"created: {sandbox_name} ({sandbox_id})")

    # Poll via the raw stub (not spawner._client.wait_ready()/.get()) so
    # status.conditions -- silently dropped by SandboxStatusRef -- is
    # visible. See module docstring.
    stub = spawner._client._stub  # pylint: disable=protected-access
    deadline = time.monotonic() + 60.0
    phase = openshell_pb2.SANDBOX_PHASE_PROVISIONING
    while time.monotonic() < deadline:
        response = await asyncio.to_thread(
            stub.GetSandbox,
            openshell_pb2.GetSandboxRequest(name=sandbox_name, workspace=WORKSPACE),
            timeout=10.0,
        )
        status = response.sandbox.status
        phase = status.phase
        print(f"phase={openshell_pb2.SandboxPhase.Name(phase)}", end="")
        for c in status.conditions:
            print(f"  condition reason={c.reason!r} message={c.message!r}", end="")
        print()
        if phase != openshell_pb2.SANDBOX_PHASE_PROVISIONING:
            break
        await asyncio.sleep(3)

    if phase != openshell_pb2.SANDBOX_PHASE_READY:
        print(
            f"Sandbox never reached READY (see issue #248) -- stopping short of the "
            f"Host-header check. Left running for inspection: {sandbox_name}"
        )
        return

    await spawner.start_server(sandbox_id, SERVER_CMD, env=SERVER_ENV)
    await asyncio.sleep(5)

    local = await asyncio.to_thread(
        spawner._client.exec,
        sandbox_id,
        [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            f"http://127.0.0.1:8080{HEALTH_PATH}",
        ],
        timeout_seconds=15,
    )
    print(
        f"in-sandbox curl 127.0.0.1:8080{HEALTH_PATH} -> exit={local.exit_code} http_code={local.stdout!r}"
    )
    if local.exit_code != 0 or local.stdout.strip() != "200":
        print(
            "App itself is not healthy inside the sandbox -- this is not the "
            "Host-header ingress gap. Left running for inspection: "
            f"{sandbox_name}"
        )
        return

    endpoint, virtual_host = await spawner._expose_service(sandbox_name, port=8080)
    print(f"ExposeService: endpoint={endpoint!r} virtual_host={virtual_host!r}")

    verify = spawner.get_query_ssl_context()
    if verify is None:
        verify = True

    results: dict[str, httpx.Response] = {}
    async with httpx.AsyncClient(timeout=5.0, verify=verify) as client:
        for label, host in [
            ("correct Host", virtual_host),
            ("wrong Host", "default--nonexistent-sandbox.openshell.localhost"),
            ("no Host", None),
        ]:
            headers = {"Host": host} if host else {}
            try:
                resp = await client.get(f"{endpoint}{HEALTH_PATH}", headers=headers)
                results[label] = resp
                print(
                    f"[{label}] status={resp.status_code} "
                    f"content-type={resp.headers.get('content-type')!r} "
                    f"body={resp.content[:200]!r}"
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                print(f"[{label}] request failed: {exc}")

    def _is_bare_404(resp: httpx.Response | None) -> bool:
        return bool(
            resp is not None
            and resp.status_code == 404
            and not resp.content
            and "content-type" not in resp.headers
        )

    correct_ok = (
        results.get("correct Host") is not None and results["correct Host"].status_code == 200
    )
    all_bare_404 = all(_is_bare_404(r) for r in results.values())

    print()
    if correct_ok:
        print("VERDICT: Host-header routing is WORKING -- correct Host reached the app.")
    elif all_bare_404:
        print(
            "VERDICT: Host-header routing is NOT functioning on this gateway's main "
            "TLS port -- correct/wrong/no Host all produced an identical bare 404. "
            "Matches the issue #209/#249 fingerprint. The app itself is confirmed "
            "healthy (see the in-sandbox curl above), so this is an ingress gap, not "
            "an app or spawner bug. Check whether this gateway exposes a separate "
            "HTTP ingress route for sandbox traffic (distinct from the main gRPC/TLS "
            "port) and point OpenShellSpawner's http_endpoint at it instead."
        )
    else:
        print(
            "VERDICT: Inconclusive/mixed -- responses differed across Host headers "
            "without the correct one succeeding. Inspect the raw responses above."
        )

    print(f"\nSandbox '{sandbox_name}' left running for further inspection.")
    print(f"  openshell sandbox exec -n {sandbox_name} -- <cmd>")
    print(f"  openshell sandbox delete {sandbox_name}   # when done")


if __name__ == "__main__":
    asyncio.run(main())
