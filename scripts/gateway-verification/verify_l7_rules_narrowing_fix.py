#!/usr/bin/env python3
"""Verify the #247 L7-rules narrowing fix against a real OpenShell gateway.

Issue #247 (and PR #257's follow-up): the bundled openai/anthropic
ProviderProfile's NetworkEndpoint used a bare access="read-write" preset,
permitting unrestricted POST/PUT/PATCH to any path on api.openai.com /
api.anthropic.com. PR #257 replaced that with an explicit L7 rules
allowlist -- but OpenShellSpawner._build_network_policy()'s separate,
sandbox-owned "llm_provider" NetworkEndpoint for the SAME host also used a
bare access="read-write" preset, and OpenShell's gateway-side policy merge
(merge_endpoint() in openshell-policy) UNIONS a bare `access` preset with
the other side's `rules` rather than letting the narrower side win: it
expands `access="read-write"` into explicit "**"-wildcard L7Rules and
appends the other side's narrower rules on top, additively. So narrowing
only the ProviderProfile's endpoint was a no-op for actual gateway
enforcement -- confirmed by reading openshell-policy/src/merge.rs, not
just asserted. This follow-up change narrows the sandbox-owned
"llm_provider" rule too, for hosts with a bundled ProviderProfile.

This script proves, against a REAL gateway (not mocks):
  (1) CreateSandbox still succeeds with BOTH NetworkEndpoints for
      api.openai.com narrowed (no FAILED_PRECONDITION, no merge-time
      rejection of two `rules`-only endpoints for the same host).
  (2) A real chat-completion call from inside the sandbox to the
      allowlisted path (POST /v1/chat/completions) succeeds end-to-end.
  (3) A call to a NON-allowlisted path on the same host (GET /v1/models)
      is actually blocked by the gateway's L7 enforcement -- proving the
      narrowing has real teeth, not just that the code compiles/doesn't
      crash CreateSandbox.

Required env vars:
  GATEWAY_URL       host:port of the OpenShell gateway (e.g. localhost:9080)
  SANDBOX_IMAGE     image with the lightspeed_agentic app + curl installed
  OPENAI_API_KEY    a real OpenAI key (never printed/logged)

Optional env vars:
  GATEWAY_WORKSPACE   default: "default"
  GATEWAY_TLS_CA      path to a CA bundle; omit for a plaintext gateway
                       (also requires OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)
  GATEWAY_BEARER_TOKEN

Usage (plaintext local-infra gateway):
  OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 \\
  GATEWAY_URL=localhost:9080 \\
  SANDBOX_IMAGE=quay.io/jameswong/lightspeed-agentic-sandbox:latest-arm64 \\
    uv run python scripts/gateway-verification/verify_l7_rules_narrowing_fix.py
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
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]


def _build_spawner():
    return build_spawner(
        "openshell",
        gateway_url=GATEWAY_URL,
        workspace=WORKSPACE,
        tls_ca=TLS_CA,
        bearer_token=BEARER_TOKEN,
    )


async def main() -> None:
    os.environ.setdefault("OPENSHELL_ALLOW_INSECURE_CREDENTIALS", "1")
    os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

    spawner = _build_spawner()
    name = "verify-l7-narrowing-247"
    try:
        endpoint = await spawner.spawn(
            name,
            SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            read_only=False,
            credential_secret_name="openai-api-key",
        )
        print(
            "PASS (1): CreateSandbox succeeded with narrowed rules on both "
            "the ProviderProfile endpoint and the sandbox-owned llm_provider "
            "endpoint for api.openai.com. endpoint:",
            endpoint,
        )

        sandbox_id = spawner.get_sandbox_id(name)

        allowed_cmd = (
            "curl -s -o /dev/null -w '%{http_code}' "
            "https://api.openai.com/v1/chat/completions "
            "-H \"Authorization: Bearer $OPENAI_API_KEY\" "
            "-H 'Content-Type: application/json' "
            '-d \'{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":1}\''
        )
        r_allowed = spawner._client.exec(
            sandbox_id, ["sh", "-c", allowed_cmd], timeout_seconds=30
        )
        allowed_code = r_allowed.stdout.strip()
        print("Allowlisted path (POST /v1/chat/completions) HTTP status:", allowed_code)
        assert allowed_code == "200", (
            f"Expected 200 from the allowlisted chat-completions path, got "
            f"{allowed_code!r}. Narrowing may have broken legitimate traffic."
        )
        print("PASS (2): allowlisted path reaches the real OpenAI API and succeeds")

        blocked_cmd = (
            "curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
            "https://api.openai.com/v1/models "
            "-H \"Authorization: Bearer $OPENAI_API_KEY\""
        )
        r_blocked = spawner._client.exec(
            sandbox_id, ["sh", "-c", blocked_cmd], timeout_seconds=30
        )
        blocked_code = r_blocked.stdout.strip()
        print("Non-allowlisted path (GET /v1/models) HTTP status:", blocked_code or "(no response / connection error)")
        # Pinned to the gateway's actual L7-deny status (403), not just
        # "!= 200" -- the same valid key already proved good on the
        # allowlisted path in check (2), so a loose "not 200" here could
        # also match an unrelated transient upstream 5xx/connection error
        # and falsely count as a pass without actually exercising gateway
        # enforcement (final-gate review note on PR #257's follow-up).
        assert blocked_code == "403", (
            f"Expected HTTP 403 (gateway L7 deny) for the non-allowlisted "
            f"GET /v1/models call, got {blocked_code!r}. Either the "
            "narrowing has no real enforcement effect (the read-write "
            "union bug in merge.rs may still be in play), or the gateway "
            "denies non-allowlisted requests with a different status than "
            "observed when this check was last run."
        )
        print(
            "PASS (3): non-allowlisted path is actually blocked "
            f"(status={blocked_code!r}) -- narrowing has real enforcement teeth"
        )

        print("\nALL CHECKS PASSED")
    finally:
        await spawner.destroy(name)
        print("destroyed", name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
