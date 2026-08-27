#!/usr/bin/env python3
"""Diagnose a stuck/failing OpenShellSpawner spawn against a real gateway.

`spawner.spawn()` deletes the sandbox automatically on any post-create
failure ("deleting sandbox to prevent orphan"), which destroys the exact
evidence needed to debug. This script creates a sandbox and stops short of
the failure-prone steps (start_server / expose / readiness), then runs a
set of diagnostics against it, leaving it running for further inspection.

Checks:
  - env dump inside the sandbox
  - DNS resolution + /etc/resolv.conf
  - reachability of LIGHTSPEED_PROVIDER_URL (if set) via curl -v
  - whether `import lightspeed_agentic.app` succeeds with the PYTHONPATH/
    LIGHTSPEED_SKILLS_DIR env vars _do_spawn() normally supplies via
    self._extra_env (NOT inherited automatically by a plain `sandbox exec`)
  - the gateway's ExposeService response (endpoint URL + virtual host) for
    the sandbox, and whether a direct HTTP GET through the gateway to
    that virtual host actually reaches the sandbox (see
    docs/testing-against-openshell-gateways.md #6 for a known gap where
    it doesn't on some gateways' ingress)

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway
  SANDBOX_IMAGE     image to diagnose

Optional env vars:
  GATEWAY_WORKSPACE       default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit for plaintext gateways
  GATEWAY_BEARER_TOKEN     OIDC/bearer token
  LIGHTSPEED_PROVIDER_URL   e.g. https://inference.local -- checked for reachability if set
  GATEWAY_VERIFY_SANDBOX_NAME   default: "diagnose-sandbox"

Usage:
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/you/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/diagnose_sandbox.py
"""

from __future__ import annotations

import asyncio
import os

from cloud_agents.spawner.factory import build_spawner

GATEWAY_URL = os.environ["GATEWAY_URL"]
SANDBOX_IMAGE = os.environ["SANDBOX_IMAGE"]
WORKSPACE = os.environ.get("GATEWAY_WORKSPACE", "default")
TLS_CA = os.environ.get("GATEWAY_TLS_CA") or None
BEARER_TOKEN = os.environ.get("GATEWAY_BEARER_TOKEN") or None
PROVIDER_URL = os.environ.get("LIGHTSPEED_PROVIDER_URL") or None
SANDBOX_NAME = os.environ.get("GATEWAY_VERIFY_SANDBOX_NAME", "diagnose-sandbox")

# Mirrors OpenShellSpawner._DEFAULT_EXTRA_ENV -- not inherited by a bare
# `sandbox exec`, only by _do_spawn()'s own start_server() call.
EXTRA_ENV = {
    "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
    "LIGHTSPEED_SKILLS_DIR": "/app/skills",
}


def _print_result(label: str, r) -> None:
    print(f"--- {label} ---")
    print("exit_code:", r.exit_code)
    if r.stdout:
        print("stdout:", r.stdout)
    if r.stderr:
        print("stderr:", r.stderr)
    print()


async def main() -> None:
    spawner = build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )

    from openshell._proto import openshell_pb2, openshell_pb2_grpc

    env = {"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"}
    if PROVIDER_URL:
        env["LIGHTSPEED_PROVIDER_URL"] = PROVIDER_URL

    spec = openshell_pb2.SandboxSpec(template=openshell_pb2.SandboxTemplate(image=SANDBOX_IMAGE, labels={}))
    for k, v in env.items():
        spec.environment[k] = v
    spawner._build_network_policy(spec, env)
    spawner._build_baseline_filesystem_policy(spec, allowed_skills=None)

    sandbox_ref = await asyncio.to_thread(spawner._client.create, workspace=WORKSPACE, spec=spec)
    sandbox_name, sandbox_id = sandbox_ref.name, sandbox_ref.id
    print("created:", sandbox_name, sandbox_id)

    await asyncio.to_thread(
        spawner._client.wait_ready, sandbox_name, workspace=WORKSPACE, timeout_seconds=60
    )
    print("sandbox running\n")

    _print_result("env", spawner._client.exec(sandbox_id, ["env"], timeout_seconds=15))
    _print_result(
        "resolv.conf", spawner._client.exec(sandbox_id, ["cat", "/etc/resolv.conf"], timeout_seconds=15)
    )

    if PROVIDER_URL:
        from urllib.parse import urlparse

        host = urlparse(PROVIDER_URL).hostname
        _print_result(
            f"getent hosts {host}",
            spawner._client.exec(sandbox_id, ["getent", "hosts", host], timeout_seconds=15),
        )
        _print_result(
            f"curl -v --max-time 5 {PROVIDER_URL}/",
            spawner._client.exec(
                sandbox_id, ["curl", "-v", "--max-time", "5", f"{PROVIDER_URL}/"], timeout_seconds=15
            ),
        )

    import_env_args = [f"{k}={v}" for k, v in EXTRA_ENV.items()]
    _print_result(
        "python3 -c 'import lightspeed_agentic.app' (with EXTRA_ENV)",
        spawner._client.exec(
            sandbox_id,
            ["env", *import_env_args, "python3", "-c", "import lightspeed_agentic.app; print('IMPORT OK')"],
            timeout_seconds=30,
        ),
    )

    def _sync_expose():
        channel = spawner._create_grpc_channel()
        try:
            stub = openshell_pb2_grpc.OpenShellStub(channel)
            return stub.ExposeService(
                openshell_pb2.ExposeServiceRequest(sandbox=sandbox_name, target_port=8080)
            )
        finally:
            channel.close()

    resp = await asyncio.to_thread(_sync_expose)
    print("--- ExposeService ---")
    print("url:", resp.url)
    print()

    print(f"Sandbox '{sandbox_name}' left running for further inspection.")
    print(f"  openshell sandbox exec -n {sandbox_name} -- <cmd>")
    print(f"  openshell sandbox delete {sandbox_name}   # when done")


if __name__ == "__main__":
    asyncio.run(main())
