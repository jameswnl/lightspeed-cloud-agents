#!/usr/bin/env python3
"""Verify the #214 fix (orphaned-Provider cleanup ordering) against a real gateway.

Issue #214: when `_do_spawn()` fails after the Provider has been created but
before the sandbox finishes coming up, the old exception handler tried to
`DeleteProvider` *before* `_cleanup_sandbox()` detached it from the sandbox --
the gateway refuses that with `FAILED_PRECONDITION: provider '<id>' is
attached to sandbox(es): <name>`, leaking the Provider. #216 reordered the
handler to run `_cleanup_sandbox()` (detach + delete sandbox) first, then
`_delete_provider()`.

This script forces exactly that failure path deterministically -- it spawns
with an image that has no lightspeed_agentic server on it (default:
`busybox:latest`), so `_wait_ready_with_host()` times out and `_do_spawn()`
raises `RuntimeError("... HTTP server did not become ready")` well after the
Provider was created and attached via `spec.providers`. This reproduces the
same exception-handler code path as the original bug report (which hit it via
the separately-tracked #209 ingress gap) without depending on a gateway that
reproduces #209 -- it works on any gateway, including ones (e.g. real OCP)
where #209 doesn't reproduce.

Checks:
  1. spawn() raises (the forced failure happened as expected).
  2. The Provider created for this run is gone from the gateway afterward
     (GetProvider returns NOT_FOUND) -- proves no orphan.
  3. `spawner._provider_ids` no longer tracks the agent -- proves internal
     bookkeeping was cleaned up, not just left stale.

Uses a fake credential value (not a real API key) -- safe to run repeatedly.

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway (e.g. localhost:8080)

Optional env vars:
  GATEWAY_WORKSPACE       default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit only for plaintext gateways
                            (also requires OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)
  GATEWAY_BEARER_TOKEN     OIDC/bearer token; see scripts/openshell-refresh-token.sh
  CREDENTIAL_SECRET_NAME    default: "openai-api-key" (k8s-normalized form)
  FAILURE_IMAGE             image with no lightspeed_agentic server; default: "busybox:latest"

Usage (real OCP, TLS + bearer token):
  GATEWAY_URL=<gateway-host>:443 \\
  GATEWAY_TLS_CA=/path/to/route-ca.pem \\
  GATEWAY_BEARER_TOKEN="$(...)" \\
    uv run python scripts/gateway-verification/verify_provider_cleanup_on_failure.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import grpc

from cloud_agents.spawner.factory import build_spawner

GATEWAY_URL = os.environ["GATEWAY_URL"]
WORKSPACE = os.environ.get("GATEWAY_WORKSPACE", "default")
TLS_CA = os.environ.get("GATEWAY_TLS_CA") or None
BEARER_TOKEN = os.environ.get("GATEWAY_BEARER_TOKEN") or None
CREDENTIAL_SECRET_NAME = os.environ.get("CREDENTIAL_SECRET_NAME", "openai-api-key")
CRED_ENV_KEY = CREDENTIAL_SECRET_NAME.upper().replace("-", "_")
FAILURE_IMAGE = os.environ.get("FAILURE_IMAGE", "busybox:latest")

FAKE_SECRET = "sk-THIS-IS-THE-FAKE-TEST-SECRET-do-not-leak"
AGENT_NAME = "verify-214-provider-cleanup"


async def main() -> None:
    if not TLS_CA:
        os.environ.setdefault("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", "1")
    os.environ[CRED_ENV_KEY] = FAKE_SECRET

    spawner = build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )

    captured: dict[str, str] = {}
    original_create_provider = spawner._create_provider

    async def _spy_create_provider(credentials: dict[str, str]) -> str:
        provider_id = await original_create_provider(credentials)
        captured["provider_id"] = provider_id
        print("Provider created for this run:", provider_id)
        return provider_id

    spawner._create_provider = _spy_create_provider  # type: ignore[method-assign]

    try:
        await spawner.spawn(
            AGENT_NAME,
            FAILURE_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name=CREDENTIAL_SECRET_NAME,
        )
        print(
            "FAIL (1): spawn() succeeded -- expected it to fail (FAILURE_IMAGE has no "
            "lightspeed_agentic server, so HTTP readiness should time out)"
        )
        sys.exit(1)
    except RuntimeError as exc:
        print("PASS (1): spawn() failed as expected:\n   ", exc)
    except Exception as exc:  # pragma: no cover - diagnostic path
        print(f"PASS (1, unexpected exception type {type(exc).__name__}, still a failure):\n   ", exc)

    provider_id = captured.get("provider_id")
    if not provider_id:
        print("FAIL: never observed a Provider being created -- cannot verify cleanup")
        sys.exit(1)

    from openshell._proto import openshell_pb2, openshell_pb2_grpc

    def _sync_get_provider():
        channel = spawner._create_grpc_channel()
        try:
            stub = openshell_pb2_grpc.OpenShellStub(channel)
            return stub.GetProvider(
                openshell_pb2.GetProviderRequest(name=provider_id, workspace=WORKSPACE)
            )
        finally:
            channel.close()

    try:
        resp = await asyncio.to_thread(_sync_get_provider)
        print(
            f"FAIL (2): provider '{provider_id}' still exists on the gateway after spawn "
            f"failure -- ORPHANED (issue #214 regression):\n   ",
            resp,
        )
        sys.exit(1)
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.NOT_FOUND:
            print(f"PASS (2): provider '{provider_id}' no longer exists on the gateway -- not orphaned")
        else:
            print(f"FAIL (2): unexpected error looking up provider '{provider_id}':", exc)
            sys.exit(1)

    if AGENT_NAME in spawner._provider_ids:
        print(
            f"FAIL (3): spawner._provider_ids still tracks '{AGENT_NAME}' -> "
            f"'{spawner._provider_ids[AGENT_NAME]}' after cleanup"
        )
        sys.exit(1)
    print(f"PASS (3): spawner._provider_ids no longer tracks '{AGENT_NAME}'")

    print("\nALL CHECKS PASSED -- #214 fix confirmed: no orphaned provider after post-create spawn failure")


if __name__ == "__main__":
    asyncio.run(main())
