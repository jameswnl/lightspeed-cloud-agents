#!/usr/bin/env python3
"""Verify the #244 provider-profile fix against a real OpenShell gateway.

Issue #244: OpenShell ships no builtin ProviderProfile for "openai"/
"anthropic" -- Provider creation with type="openai" succeeds regardless,
but the gateway silently skips both credential-env-var injection and
network-egress policy for that provider (gateway logs "provider type has
no profile; skipping provider policy layer"). OpenShellSpawner now calls
_ensure_provider_profile() before _create_provider() to idempotently
import a bundled profile when the gateway has none.

This check confirms, against a REAL gateway (not mocks): after calling
_ensure_provider_profile("openai"), a ListProviderProfiles call scoped to
this workspace actually returns a profile with id "openai" -- proving the
import round-tripped through the real gRPC service, not just that the
client-side call didn't raise.

Uses a fake credential value (not a real API key) -- this only verifies
profile registration and that a spawn with LIGHTSPEED_PROVIDER=openai
succeeds, not that a real LLM call succeeds end-to-end (that requires a
real OPENAI_API_KEY; see docs/testing-against-openshell-gateways.md
section 3 for that separate, manual check).

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway (e.g. localhost:8080)
  SANDBOX_IMAGE    image with the lightspeed_agentic app installed

Optional env vars:
  GATEWAY_WORKSPACE       default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit for a plaintext gateway
                            (also requires OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)
  GATEWAY_BEARER_TOKEN     OIDC/bearer token; see scripts/openshell-refresh-token.sh

Usage (plaintext local-infra gateway):
  cd ~/ws/local-infra && make up-openshell
  OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 \\
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/you/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/verify_provider_profile_fix.py
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

FAKE_SECRET = "sk-THIS-IS-THE-FAKE-TEST-SECRET-do-not-leak"


def _build_spawner():
    return build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )


def _list_profile_ids(spawner) -> set[str]:
    from openshell._proto import openshell_pb2, openshell_pb2_grpc

    channel = spawner._create_grpc_channel()
    try:
        stub = openshell_pb2_grpc.OpenShellStub(channel)
        resp = stub.ListProviderProfiles(
            openshell_pb2.ListProviderProfilesRequest(workspace=WORKSPACE)
        )
        return {p.id for p in resp.profiles}
    finally:
        channel.close()


async def check_ensure_provider_profile_registers_openai() -> None:
    spawner = _build_spawner()

    before = _list_profile_ids(spawner)
    print("Profiles visible before:", sorted(before) or "(none)")

    await spawner._ensure_provider_profile("openai")

    after = _list_profile_ids(spawner)
    print("Profiles visible after:", sorted(after))

    assert "openai" in after, (
        "ListProviderProfiles does not include 'openai' after "
        "_ensure_provider_profile('openai') -- import did not round-trip "
        "through the real gateway"
    )
    print("PASS (1): gateway now has a registered 'openai' ProviderProfile")

    # Idempotency: calling again must not raise or duplicate the profile.
    await spawner._ensure_provider_profile("openai")
    after_again = _list_profile_ids(spawner)
    assert after_again == after, (
        f"second _ensure_provider_profile call changed the profile set: "
        f"{after} -> {after_again}"
    )
    print("PASS (2): second call is a no-op (idempotent)")


async def check_spawn_with_openai_provider_succeeds() -> None:
    os.environ.setdefault("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", "1")
    os.environ["OPENAI_API_KEY"] = FAKE_SECRET

    spawner = _build_spawner()
    name = "verify-profile-fix-spawn"
    try:
        endpoint = await spawner.spawn(
            name,
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name="openai-api-key",
        )
        print("PASS (3): spawn with LIGHTSPEED_PROVIDER=openai succeeded. endpoint:", endpoint)
        print("\nALL CHECKS PASSED")
    finally:
        await spawner.destroy(name)
        print("destroyed", name)


async def main() -> None:
    await check_ensure_provider_profile_registers_openai()
    await check_spawn_with_openai_provider_succeeds()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
