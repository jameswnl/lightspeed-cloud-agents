"""Build AgentSpawner instances from explicit parameters.

Given a spawner type and its constructor parameters, dispatches to the
right AgentSpawner implementation. Callers with their own configuration
system (e.g. lightspeed-stack's Pydantic SpawnerConfiguration) call this
directly with plain kwargs; env-var-based callers (the Temporal entrypoint)
read os.environ themselves and forward the resulting values here.

Spawner implementations are imported lazily per branch so that installing
only one spawner's extras (kubernetes, podman, openshell) doesn't require
the others' dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cloud_agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)

_KNOWN_SPAWNER_TYPES = ("kubernetes", "podman", "openshell")


def build_spawner(spawner_type: str, **params: Any) -> "AgentSpawner":
    """Build an AgentSpawner instance for the given type.

    Parameters:
        spawner_type: One of "kubernetes", "podman", "openshell".
        **params: Constructor parameters for the corresponding spawner.
            For "kubernetes": namespace, service_account, etc. -- forwarded
                directly to KubernetesSpawner.
            For "podman": network, volume_mounts, etc. -- forwarded
                directly to PodmanSpawner.
            For "openshell": gateway_url, driver, workspace, http_endpoint,
                tls_ca, tls_cert, tls_key, bearer_token -- used to build the
                underlying SandboxClient (with TLS/bearer auth) and then
                OpenShellSpawner.

    Returns:
        A configured AgentSpawner instance.

    Raises:
        ValueError: If spawner_type is not one of the known types. Unlike
            the env-var-driven entrypoint (where an unset/empty type means
            "no spawner configured"), a caller invoking this function
            directly with an unrecognized type is a bug, not a valid
            "disabled" state -- so this raises rather than returning None.
    """
    if spawner_type == "kubernetes":
        from cloud_agents.spawner.kubernetes_spawner import KubernetesSpawner

        logger.info(
            "Using KubernetesSpawner (namespace=%s)", params.get("namespace", "cloud-agents")
        )
        return KubernetesSpawner(**params)
    if spawner_type == "podman":
        from cloud_agents.spawner.podman_spawner import PodmanSpawner

        logger.info("Using PodmanSpawner (network=%s)", params.get("network", "cloud-agents"))
        return PodmanSpawner(**params)
    if spawner_type == "openshell":
        return _build_openshell_spawner(**params)
    raise ValueError(
        f"Unknown spawner_type {spawner_type!r}; expected one of {_KNOWN_SPAWNER_TYPES}"
    )


def _build_openshell_spawner(
    gateway_url: str | None = None,
    driver: str | None = None,
    workspace: str | None = None,
    http_endpoint: str | None = None,
    tls_ca: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    bearer_token: str | None = None,
    **kwargs: Any,
) -> "AgentSpawner":
    """Build an OpenShellSpawner, including SandboxClient auth wiring.

    Falsy/None values fall back to the same defaults the Temporal
    entrypoint used to read from os.environ, so callers whose own config
    models default optional fields to None (e.g. Pydantic) behave the
    same as unset env vars.
    """
    from cloud_agents.spawner.openshell_spawner import OpenShellSpawner
    from openshell import SandboxClient

    gateway_url = gateway_url or "localhost:17670"
    driver = driver or "podman"
    workspace = workspace or "default"
    http_endpoint = http_endpoint or ""
    tls_ca = tls_ca or ""
    tls_cert = tls_cert or ""
    tls_key = tls_key or ""
    bearer_token = bearer_token or ""

    # Strip http(s):// scheme -- SandboxClient uses gRPC, not HTTP
    grpc_endpoint = gateway_url.replace("http://", "").replace("https://", "")

    client_kwargs: dict[str, Any] = {"endpoint": grpc_endpoint}
    if tls_ca:
        from pathlib import Path

        from openshell import TlsConfig

        tls_config = TlsConfig(ca_path=Path(tls_ca))
        if tls_cert and tls_key:
            tls_config = TlsConfig(
                ca_path=Path(tls_ca),
                cert_path=Path(tls_cert),
                key_path=Path(tls_key),
            )
        client_kwargs["tls"] = tls_config
        logger.info("OpenShell TLS enabled (ca=%s)", tls_ca)
    if bearer_token:
        client_kwargs["bearer_token"] = bearer_token
        logger.info("OpenShell bearer token auth enabled")

    client = SandboxClient(**client_kwargs)
    logger.info(
        "Using OpenShellSpawner (gateway=%s, driver=%s, workspace=%s)",
        gateway_url,
        driver,
        workspace,
    )

    return OpenShellSpawner(
        openshell_client=client,
        driver=driver,
        workspace=workspace,
        endpoint=grpc_endpoint,
        http_endpoint=http_endpoint,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        bearer_token=bearer_token,
        **kwargs,
    )
