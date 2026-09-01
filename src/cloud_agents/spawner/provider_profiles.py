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
                    access="read-write",
                    enforcement="enforce",
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
        ),
        "anthropic": _profile(
            "anthropic", "Anthropic", "Anthropic inference endpoints",
            "ANTHROPIC_API_KEY", "api.anthropic.com",
        ),
    }
