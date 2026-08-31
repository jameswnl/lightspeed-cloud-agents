"""Build AgentSpawner instances from explicit parameters.

Given a spawner type and its constructor parameters, dispatches to the
right AgentSpawner implementation. Callers with their own configuration
system (e.g. lightspeed-stack's Pydantic SpawnerConfiguration) call this
directly with plain kwargs; env-var-based callers (the Temporal entrypoint)
read os.environ themselves and forward the resulting values here.

OpenShellSpawner is the only supported ephemeral spawner (issue #198) --
Kubernetes and Podman deployment targets are reached through OpenShell's
own gateway compute driver, not through separate AgentSpawner
implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cloud_agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)

_KNOWN_SPAWNER_TYPES = ("openshell",)

# Explicit allowlist (not signature introspection): OpenShellSpawner's
# __init__ forwards unrecognized kwargs to AgentSpawner.__init__(max_pods=...),
# so an unknown key doesn't fail loudly at the spawner's own constructor --
# it fails at the base class instead, several frames away from the real
# cause. Filtering here lets a caller pass a broader config object (e.g. a
# lightspeed-stack Pydantic model_dump() with `type`/`sandbox_image`/etc.
# mixed in) without needing to hand-pick fields first.
_OPENSHELL_EXTRA_PARAMS = frozenset({"max_pods", "extra_readable_paths", "extra_env"})


def _filtered(params: dict[str, Any], known: frozenset[str]) -> dict[str, Any]:
    """Drop keys not in `known` and values that are explicitly None.

    None-dropping matters because Pydantic Optional fields default to None
    rather than being omitted -- passing e.g. workspace=None to
    OpenShellSpawner would store None instead of falling back to its own
    class default.
    """
    return {k: v for k, v in params.items() if k in known and v is not None}


def build_spawner(spawner_type: str, **params: Any) -> "AgentSpawner":
    """Build an AgentSpawner instance for the given type.

    Parameters:
        spawner_type: Must be "openshell" -- the only supported ephemeral
            spawner (issue #198).
        **params: Constructor parameters for OpenShellSpawner: gateway_url,
            workspace, http_endpoint, tls_ca, tls_cert, tls_key,
            bearer_token, bearer_token_provider, max_pods,
            extra_readable_paths, extra_env -- used to build the
            underlying SandboxClient (with TLS/bearer auth) and then
            OpenShellSpawner.
            Callers may pass a broader dict (e.g. a Pydantic model_dump())
            containing extra keys -- unrecognized keys and explicit None
            values are dropped rather than forwarded, so passing an unset
            Optional field or an unrelated config field (`type`,
            `sandbox_image`, ...) doesn't crash or override a spawner's own
            default with None.

    Returns:
        A configured AgentSpawner instance.

    Raises:
        ValueError: If spawner_type is not "openshell". Unlike the
            env-var-driven entrypoint (where an unset/empty type means
            "no spawner configured"), a caller invoking this function
            directly with an unrecognized type is a bug, not a valid
            "disabled" state -- so this raises rather than returning None.
    """
    if spawner_type == "openshell":
        return _build_openshell_spawner(**params)
    raise ValueError(
        f"Unknown spawner_type {spawner_type!r}; expected one of {_KNOWN_SPAWNER_TYPES}"
    )


def _build_openshell_spawner(
    gateway_url: str | None = None,
    workspace: str | None = None,
    http_endpoint: str | None = None,
    tls_ca: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    bearer_token: str | None = None,
    bearer_token_provider: Callable[[], str] | None = None,
    **kwargs: Any,
) -> "AgentSpawner":
    """Build an OpenShellSpawner, including SandboxClient auth wiring.

    Falsy/None values fall back to the same defaults the Temporal
    entrypoint used to read from os.environ, so callers whose own config
    models default optional fields to None (e.g. Pydantic) behave the
    same as unset env vars.

    bearer_token_provider is a zero-arg callable returning a fresh bearer
    token string, forwarded as-is to both the underlying SandboxClient
    (which already supports a callable `bearer_token`, re-invoking it per
    RPC) and OpenShellSpawner (see its own bearer_token_provider docstring
    for why this matters: a short-lived OIDC token would otherwise expire
    mid-spawn with no way to refresh -- issue #236). Mutually exclusive
    with bearer_token -- OpenShellSpawner's constructor raises ValueError
    if both are set.
    """
    from cloud_agents.spawner.openshell_spawner import OpenShellSpawner
    from openshell import SandboxClient

    gateway_url = gateway_url or "localhost:17670"
    workspace = workspace or "default"
    http_endpoint = http_endpoint or ""
    tls_ca = tls_ca or ""
    tls_cert = tls_cert or ""
    tls_key = tls_key or ""
    bearer_token = bearer_token or ""

    if bearer_token and bearer_token_provider is not None:
        # Fail closed before constructing SandboxClient -- a single
        # fail-closed story, not "build a client with the static token,
        # then discover the ambiguity only once OpenShellSpawner's own
        # (otherwise-identical) constructor check raises."
        raise ValueError(
            "bearer_token and bearer_token_provider are mutually exclusive -- "
            "pass one or the other, not both"
        )

    # Strip http(s):// scheme -- SandboxClient uses gRPC, not HTTP
    grpc_endpoint = gateway_url.replace("http://", "").replace("https://", "")

    client_kwargs: dict[str, Any] = {"endpoint": grpc_endpoint}
    if tls_ca:
        from pathlib import Path

        from openshell import TlsConfig

        tls_kwargs: dict[str, Any] = {"ca_path": Path(tls_ca)}
        if tls_cert and tls_key:
            tls_kwargs["cert_path"] = Path(tls_cert)
            tls_kwargs["key_path"] = Path(tls_key)
        client_kwargs["tls"] = TlsConfig(**tls_kwargs)
        logger.info("OpenShell TLS enabled (ca=%s)", tls_ca)
    if bearer_token:
        client_kwargs["bearer_token"] = bearer_token
        logger.info("OpenShell bearer token auth enabled")
    elif bearer_token_provider is not None:
        client_kwargs["bearer_token"] = bearer_token_provider
        logger.info("OpenShell bearer token provider auth enabled (auto-refreshing)")

    client = SandboxClient(**client_kwargs)
    logger.info(
        "Using OpenShellSpawner (gateway=%s, workspace=%s)",
        gateway_url,
        workspace,
    )

    return OpenShellSpawner(
        openshell_client=client,
        workspace=workspace,
        endpoint=grpc_endpoint,
        http_endpoint=http_endpoint,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        bearer_token=bearer_token,
        bearer_token_provider=bearer_token_provider,
        **_filtered(kwargs, _OPENSHELL_EXTRA_PARAMS),
    )
