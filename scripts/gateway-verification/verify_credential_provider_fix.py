#!/usr/bin/env python3
"""Verify the #199 credential-exposure fix against a real OpenShell gateway.

Two checks:
  1. Fail-closed by default: Provider creation over a non-TLS gateway must
     raise, unless OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 is set.
  2. With that opt-in set (needed for plaintext local/dev gateways): spawn
     succeeds, and the real credential value is verifiably ABSENT from
     every process's environment inside the sandbox -- it must only be
     resolvable via the gateway's Provider placeholder mechanism.

Uses a fake credential value (not a real API key) so this is safe to run
repeatedly and doesn't require real LLM access -- it only checks exposure,
not that the placeholder actually resolves to a working call. For gateways
with TLS already configured, check (1) is expected to be skipped (nothing
to fail closed on) -- run this against a plaintext gateway (e.g.
local-infra's `make up-openshell`) to exercise it.

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway (e.g. localhost:8080)
  SANDBOX_IMAGE     image with the lightspeed_agentic app installed

Optional env vars:
  GATEWAY_WORKSPACE       default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit to test the plaintext/fail-closed path
  GATEWAY_BEARER_TOKEN     OIDC/bearer token; see scripts/openshell-refresh-token.sh
  CREDENTIAL_SECRET_NAME    default: "openai-api-key" (k8s-normalized form)

Usage (plaintext local-infra gateway, exercises both checks):
  cd ~/ws/local-infra && make up-openshell
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/you/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/verify_credential_provider_fix.py
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
CREDENTIAL_SECRET_NAME = os.environ.get("CREDENTIAL_SECRET_NAME", "openai-api-key")
CRED_ENV_KEY = CREDENTIAL_SECRET_NAME.upper().replace("-", "_")

FAKE_SECRET = "sk-THIS-IS-THE-FAKE-TEST-SECRET-do-not-leak"


def _build_spawner():
    return build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )


async def check_fails_closed_without_tls() -> bool:
    """Returns True if the fail-closed check ran (i.e. gateway has no TLS configured)."""
    if TLS_CA:
        print("SKIP (1): GATEWAY_TLS_CA is set -- nothing to fail closed on. "
              "Run against a plaintext gateway to exercise this check.")
        return False

    os.environ.pop("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", None)
    os.environ[CRED_ENV_KEY] = FAKE_SECRET

    spawner = _build_spawner()
    try:
        await spawner.spawn(
            "verify-cred-fix-closed",
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name=CREDENTIAL_SECRET_NAME,
        )
        print("FAIL (1): spawn succeeded without TLS and without opt-in -- should have raised")
        raise AssertionError("credential provider creation did not fail closed")
    except ValueError as exc:
        assert "TLS" in str(exc) or "insecure" in str(exc), f"unexpected error: {exc}"
        print("PASS (1): refused to send credentials over an insecure channel:\n   ", exc)
        return True
    finally:
        try:
            await spawner.destroy("verify-cred-fix-closed")
        except Exception:
            pass


async def check_credential_not_exposed() -> None:
    os.environ["OPENSHELL_ALLOW_INSECURE_CREDENTIALS"] = "1"
    os.environ[CRED_ENV_KEY] = FAKE_SECRET

    spawner = _build_spawner()
    name = "verify-cred-fix-exposure"
    try:
        endpoint = await spawner.spawn(
            name,
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name=CREDENTIAL_SECRET_NAME,
        )
        print("PASS (2a): spawn succeeded. endpoint:", endpoint)

        sandbox_id = spawner.get_sandbox_id(name)

        r = spawner._client.exec(sandbox_id, ["env"], timeout_seconds=15)
        assert FAKE_SECRET not in r.stdout, "REAL SECRET LEAKED into sandbox baseline env!"
        print("PASS (2b): real credential value is NOT present in the sandbox's baseline env")

        # The agent server itself runs as a separately exec'd process (start_server);
        # check every process's own environment via /proc, not just the exec shell's.
        r2 = spawner._client.exec(
            sandbox_id,
            ["sh", "-c", "for p in /proc/[0-9]*/environ; do tr '\\0' '\\n' < $p 2>/dev/null; done"],
            timeout_seconds=15,
        )
        assert FAKE_SECRET not in r2.stdout, "REAL SECRET LEAKED into a sandboxed process's env!"
        print("PASS (2c): real credential value is NOT present in any process env in the sandbox")

        print("\nALL CHECKS PASSED")
    finally:
        await spawner.destroy(name)
        print("destroyed", name)


async def main() -> None:
    await check_fails_closed_without_tls()
    await check_credential_not_exposed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
