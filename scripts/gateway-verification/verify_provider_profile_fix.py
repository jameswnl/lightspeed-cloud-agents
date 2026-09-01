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

Check (1)/(2) below need no credential at all -- they call
_ensure_provider_profile() directly and only touch the profile catalog,
which fully proves the #244 fix's own logic.

Check (3)/(4) proves the deeper end-to-end claim (a registered profile
actually results in usable credential injection, not just profile
registration) but requires a REAL OPENAI_API_KEY via the REAL_OPENAI_API_KEY
env var, and is skipped otherwise. A fake key cannot be used here even
though verify_credential_provider_fix.py uses one for the analogous #199
check: confirmed live against a real gateway (PR #246 review) that
SetInferenceRoute performs live upstream credential verification once a
provider profile is registered, so a fake key now gets rejected with 401
before a sandbox is ever created -- there is no client-side way to fake
past that, and there shouldn't be (it's a real security feature, not a
testing inconvenience). When run with a real key, this check asserts the
real value's ABSENCE from every sandboxed process's raw env (issue #199's
invariant, same direction as verify_credential_provider_fix.py) -- never
its presence. A previous revision of this script incorrectly asserted a
fake secret's PRESENCE in raw env to "prove" injection, which is the
opposite of what #199 guarantees; that was caught in PR #246 review before
merge.

Required env vars:
  GATEWAY_URL      host:port of the OpenShell gateway (e.g. localhost:8080)
  SANDBOX_IMAGE    image with the lightspeed_agentic app installed

Optional env vars:
  GATEWAY_WORKSPACE        default: "default"
  GATEWAY_TLS_CA           path to a CA bundle; omit for a plaintext gateway
                            (also requires OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)
  GATEWAY_BEARER_TOKEN     OIDC/bearer token; see scripts/openshell-refresh-token.sh
  REAL_OPENAI_API_KEY      a real OpenAI key; enables check (3)/(4). Never
                           printed or logged -- only asserted absent from
                           sandboxed process env.

Usage (plaintext local-infra gateway, profile-registration checks only):
  cd ~/ws/local-infra && make up-openshell
  OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 \\
  GATEWAY_URL=localhost:8080 \\
  SANDBOX_IMAGE=quay.io/you/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/verify_provider_profile_fix.py

Usage (add the real-key injection proof):
  REAL_OPENAI_API_KEY=sk-... \\
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


async def check_credential_not_exposed_with_real_key() -> None:
    """Prove #199's exposure invariant still holds for a profile-registered provider type.

    This is the deeper, optional proof that #244's fix results in *usable*
    credential injection, not just a registered profile -- but it can only
    run with a real key (see module docstring for why a fake one no longer
    reaches sandbox creation at all once a profile exists). It intentionally
    asserts the real value's ABSENCE from every sandboxed process's raw env,
    matching verify_credential_provider_fix.py's #199 check -- the real
    credential must only ever be resolved server-side via the
    openshell:resolve:env:OPENAI_API_KEY placeholder, never appear literally
    in the sandbox. Asserting PRESENCE instead (a prior revision of this
    function did, to "prove" injection) would flag success on data leakage
    and is the wrong direction entirely -- caught in PR #246 review.
    """
    real_key = os.environ.get("REAL_OPENAI_API_KEY")
    if not real_key:
        print(
            "SKIP (3/4): set REAL_OPENAI_API_KEY to also prove credential "
            "injection end-to-end. SetInferenceRoute live-verifies "
            "credentials against the real upstream once a provider profile "
            "is registered, so a fake key can no longer reach sandbox "
            "creation at all -- this check needs a real one."
        )
        return

    os.environ.setdefault("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", "1")
    os.environ["OPENAI_API_KEY"] = real_key

    spawner = _build_spawner()
    name = "verify-profile-fix-real-key"
    try:
        endpoint = await spawner.spawn(
            name,
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name="openai-api-key",
        )
        print(
            "PASS (3): spawn with LIGHTSPEED_PROVIDER=openai and a real key "
            "succeeded (proves SetInferenceRoute's live upstream "
            "verification accepted it). endpoint:",
            endpoint,
        )

        sandbox_id = spawner.get_sandbox_id(name)
        r = spawner._client.exec(
            sandbox_id,
            ["sh", "-c", "for p in /proc/[0-9]*/environ; do tr '\\0' '\\n' < $p 2>/dev/null; done"],
            timeout_seconds=15,
        )
        assert real_key not in r.stdout, (
            "REAL SECRET LEAKED into a sandboxed process's env! Issue #199's "
            "guarantee does not hold for a provider type with a registered "
            "ProviderProfile (#244)."
        )
        print(
            "PASS (4): the real credential value is NOT present in any "
            "sandboxed process's env -- #199's exposure guarantee holds "
            "under #244's fix, for a real, upstream-verified credential"
        )
        print(
            "\nFor the strongest possible proof (a real LLM call actually "
            "succeeding end-to-end), see docs/testing-against-openshell-"
            "gateways.md section 3."
        )
        print("\nALL CHECKS PASSED")
    finally:
        await spawner.destroy(name)
        print("destroyed", name)


async def main() -> None:
    await check_ensure_provider_profile_registers_openai()
    await check_credential_not_exposed_with_real_key()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
