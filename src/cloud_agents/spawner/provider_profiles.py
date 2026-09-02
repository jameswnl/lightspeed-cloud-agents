"""Bundled OpenShell ProviderProfile definitions for issue #244.

OpenShell's builtin provider-profile catalog ships profiles for
aws/aws-bedrock/aws-s3/claude-code/codex/copilot/cursor/deepinfra/github/
google-cloud/google-vertex-ai/nvidia/pypi, but NOT for "openai" or
"anthropic" -- even though both are recognized, routable inference
provider type strings. A Provider with no matching ProviderProfile still
creates successfully, but the gateway silently skips both credential
env-var injection and network-egress policy for it (see
OpenShellSpawner._ensure_provider_profile). These bundled definitions
give OpenShellSpawner something to import so those two types work the
same way the profiled builtins already do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openshell._proto import openshell_pb2


def bundled_provider_profiles() -> dict[str, "openshell_pb2.ProviderProfile"]:
    """Return bundled ProviderProfile messages keyed by provider type id."""
    from openshell._proto import openshell_pb2, sandbox_pb2

    def _profile(
        profile_id: str,
        display_name: str,
        description: str,
        env_var: str,
        host: str,
        allowed_requests: list[tuple[str, str]],
    ) -> "openshell_pb2.ProviderProfile":
        return openshell_pb2.ProviderProfile(
            id=profile_id,
            display_name=display_name,
            description=description,
            category=openshell_pb2.PROVIDER_PROFILE_CATEGORY_INFERENCE,
            inference_capable=True,
            credentials=[
                openshell_pb2.ProviderProfileCredential(
                    name="api_key",
                    description=f"{display_name} API key",
                    env_vars=[env_var],
                    required=True,
                    auth_style="bearer",
                    header_name="authorization",
                )
            ],
            discovery=openshell_pb2.ProviderProfileDiscovery(credentials=["api_key"]),
            endpoints=[
                sandbox_pb2.NetworkEndpoint(
                    host=host,
                    port=443,
                    protocol="rest",
                    enforcement="enforce",
                    # Explicit L7 allowlist (issue #247), not the broad
                    # `access: "read-write"` preset -- that permitted
                    # unrestricted POST/PUT/PATCH to any path on this host,
                    # not just the inference endpoints the sandboxed LLM
                    # client actually calls (CodeRabbit finding on PR #246).
                    # `access` and `rules` are mutually exclusive
                    # (proto/sandbox.proto), so this omits `access` entirely.
                    rules=[
                        sandbox_pb2.L7Rule(
                            allow=sandbox_pb2.L7Allow(method=method, path=path)
                        )
                        for method, path in allowed_requests
                    ],
                )
            ],
            binaries=[
                sandbox_pb2.NetworkBinary(path="/usr/bin/curl"),
                sandbox_pb2.NetworkBinary(path="/usr/local/bin/curl"),
            ],
        )

    return {
        "openai": _profile(
            "openai", "OpenAI", "OpenAI inference endpoints",
            "OPENAI_API_KEY", "api.openai.com",
            # pydantic-ai's OpenAIChatModel (default for a bare "openai:..."
            # model string) posts to /v1/chat/completions; OpenAIResponsesModel
            # (only used if a step explicitly configures "openai-responses:...")
            # posts to /v1/responses. Both are allowed since either could be
            # configured.
            allowed_requests=[
                ("POST", "/v1/chat/completions"),
                ("POST", "/v1/responses"),
            ],
        ),
        "anthropic": _profile(
            "anthropic", "Anthropic", "Anthropic inference endpoints",
            "ANTHROPIC_API_KEY", "api.anthropic.com",
            # pydantic-ai's AnthropicModel posts to /v1/messages (via
            # client.beta.messages.create -- "beta" is a `?beta=true` query
            # param on that same path, not a different route). Does not
            # cover /v1/messages/count_tokens: that's only hit if a step
            # sets UsageLimits.count_tokens_before_request=True, which
            # defaults to False and isn't used anywhere in this codebase.
            allowed_requests=[("POST", "/v1/messages")],
        ),
    }
