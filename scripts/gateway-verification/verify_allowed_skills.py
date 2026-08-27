#!/usr/bin/env python3
"""Verify `allowed_skills` Landlock scoping against a real OpenShell gateway.

Spawns a sandbox with `allowed_skills=[ALLOWED_SKILL]` and asserts:
  1. ALLOWED_SKILL's SKILL.md is readable inside the sandbox.
  2. DENIED_SKILL's SKILL.md is NOT readable (Landlock EACCES).
  3. The materialized /app/skills listing contains only ALLOWED_SKILL.

Two modes:
  - Full (default): the real `spawner.spawn()` lifecycle, including the
    HTTP server readiness check. Requires the gateway to route HTTP
    Host-header-multiplexed traffic to the sandbox (works on local-infra,
    Kind, and real OCP as of this writing -- see
    docs/testing-against-openshell-gateways.md #6 for gateways where it
    doesn't).
  - Bypass (GATEWAY_VERIFY_BYPASS_READY=1): replicates `_do_spawn()`'s
    steps directly against the low-level client up through
    `_materialize_allowed_skills()`, skipping `start_server()`/
    `_expose_service()`/readiness. Use this when the gateway's ingress
    doesn't support the HTTP exposure path but you still need to verify
    the sandbox-internal Landlock scoping.

Required env vars:
  GATEWAY_URL          host:port of the OpenShell gateway (e.g. localhost:8080)
  SANDBOX_IMAGE         image with skills baked in (e.g. the
                        lightspeed-agentic-sandbox image with skills/ and
                        scripts/materialize-skills.sh)

Optional env vars:
  GATEWAY_WORKSPACE           default: "default"
  GATEWAY_TLS_CA               path to a CA bundle; omit for plaintext gateways
  GATEWAY_BEARER_TOKEN          OIDC/bearer token; see
                                scripts/openshell-refresh-token.sh
  ALLOWED_SKILL                 default: "k8s-diag"
  DENIED_SKILL                  default: "git-ops"
  GATEWAY_VERIFY_BYPASS_READY   "1" to skip start_server()/readiness (see above)
  GATEWAY_VERIFY_SANDBOX_NAME   default: "verify-allowed-skills"
  GATEWAY_VERIFY_KEEP_RUNNING   "1" to leave the sandbox running for inspection

Usage:
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/you/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/verify_allowed_skills.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from cloud_agents.spawner.factory import build_spawner

GATEWAY_URL = os.environ["GATEWAY_URL"]
SANDBOX_IMAGE = os.environ["SANDBOX_IMAGE"]
WORKSPACE = os.environ.get("GATEWAY_WORKSPACE", "default")
TLS_CA = os.environ.get("GATEWAY_TLS_CA") or None
BEARER_TOKEN = os.environ.get("GATEWAY_BEARER_TOKEN") or None
ALLOWED_SKILL = os.environ.get("ALLOWED_SKILL", "k8s-diag")
DENIED_SKILL = os.environ.get("DENIED_SKILL", "git-ops")
BYPASS_READY = os.environ.get("GATEWAY_VERIFY_BYPASS_READY") == "1"
SANDBOX_NAME = os.environ.get("GATEWAY_VERIFY_SANDBOX_NAME", "verify-allowed-skills")
KEEP_RUNNING = os.environ.get("GATEWAY_VERIFY_KEEP_RUNNING") == "1"


def _exec(client, sandbox_id: str, cmd: list[str]):
    return client.exec(sandbox_id, cmd, timeout_seconds=15)


async def _run_checks(client, sandbox_id: str) -> None:
    r1 = _exec(client, sandbox_id, ["cat", f"/skills/{ALLOWED_SKILL}/SKILL.md"])
    print(f"(1) read allowed skill '{ALLOWED_SKILL}' -- exit_code:", r1.exit_code)
    assert r1.exit_code == 0, f"expected {ALLOWED_SKILL} readable"

    r2 = _exec(client, sandbox_id, ["cat", f"/skills/{DENIED_SKILL}/SKILL.md"])
    print(f"(2) read unlisted skill '{DENIED_SKILL}' -- exit_code:", r2.exit_code, "stderr:", r2.stderr)
    assert r2.exit_code != 0, f"expected {DENIED_SKILL} NOT readable"

    r3 = _exec(client, sandbox_id, ["ls", "/app/skills"])
    print("(3) materialized listing:", repr(r3.stdout))
    assert ALLOWED_SKILL in r3.stdout
    assert DENIED_SKILL not in r3.stdout

    print("\nALL CHECKS PASSED")


async def run_full() -> None:
    spawner = build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )
    try:
        endpoint = await spawner.spawn(
            SANDBOX_NAME,
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            allowed_skills=[ALLOWED_SKILL],
        )
        print("SPAWN SUCCEEDED, endpoint:", endpoint)
        sandbox_id = spawner.get_sandbox_id(SANDBOX_NAME)
        await _run_checks(spawner._client, sandbox_id)
    finally:
        if KEEP_RUNNING:
            print(f"\nLeaving '{SANDBOX_NAME}' running per GATEWAY_VERIFY_KEEP_RUNNING=1.")
        else:
            await spawner.destroy(SANDBOX_NAME)
            print("destroyed", SANDBOX_NAME)


async def run_bypass() -> None:
    """Replicate _do_spawn() up through _materialize_allowed_skills(), skipping HTTP readiness.

    Use when the gateway's ingress doesn't support Host-header-multiplexed
    HTTP proxying (see docs/testing-against-openshell-gateways.md #6) --
    this still fully exercises the Landlock scoping this script checks.
    """
    from openshell._proto import openshell_pb2

    spawner = build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )
    spec = openshell_pb2.SandboxSpec(template=openshell_pb2.SandboxTemplate(image=SANDBOX_IMAGE, labels={}))
    env = {"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"}
    for k, v in env.items():
        spec.environment[k] = v
    spawner._build_network_policy(spec, env)
    spawner._build_baseline_filesystem_policy(spec, allowed_skills=[ALLOWED_SKILL])

    sandbox_ref = await asyncio.to_thread(spawner._client.create, workspace=WORKSPACE, spec=spec)
    sandbox_name, sandbox_id = sandbox_ref.name, sandbox_ref.id
    print("created:", sandbox_name)
    try:
        await asyncio.to_thread(
            spawner._client.wait_ready, sandbox_name, workspace=WORKSPACE, timeout_seconds=60
        )
        print("sandbox running")
        await spawner._materialize_allowed_skills(sandbox_id, [ALLOWED_SKILL])
        print("materialize-skills.sh succeeded")
        await _run_checks(spawner._client, sandbox_id)
    finally:
        if KEEP_RUNNING:
            print(f"\nLeaving '{sandbox_name}' running per GATEWAY_VERIFY_KEEP_RUNNING=1.")
        else:
            await asyncio.to_thread(spawner._client.delete, sandbox_name, workspace=WORKSPACE)
            print("destroyed", sandbox_name)


if __name__ == "__main__":
    try:
        asyncio.run(run_bypass() if BYPASS_READY else run_full())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
