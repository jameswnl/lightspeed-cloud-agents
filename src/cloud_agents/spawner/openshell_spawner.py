"""OpenShell Gateway agent spawner -- creates sandboxes via OpenShell gRPC API.

Spike prototype (issue #50). Uses the OpenShell Python SDK to create
sandboxes through the OpenShell Gateway, which provides a unified
backend for Docker, Podman, K8s, and MicroVM runtimes with built-in
Landlock + seccomp + network namespace isolation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from cloud_agents.spawner.base import AgentSpawner, SpawnConfig

if TYPE_CHECKING:
    from cloud_agents.workflow.tls import EphemeralCerts

logger = logging.getLogger(__name__)

# All sandboxes created by this spawner use this name prefix.
# Used for filtering in _do_list_active since SandboxRef has no labels.
SANDBOX_NAME_PREFIX = "ca-agent-"

# Default port for the sandbox HTTP server.
SANDBOX_HTTP_PORT = 8080

# Service name used when registering with ExposeService.
SANDBOX_SERVICE_NAME = "agent-http"


class OpenShellSpawner(AgentSpawner):
    """Spawns sandboxes via the OpenShell Gateway gRPC API.

    This is a spike prototype that replaces both KubernetesSpawner and
    PodmanSpawner with a single implementation backed by OpenShell Gateway.

    The spawner uses Option C from the spike design: start the sandbox with
    our sandbox image (lightspeed-agentic-sandbox), expose its HTTP port
    via OpenShell's ExposeService, and communicate through the gateway-
    provided URL to preserve the POST /v1/agent/run contract.

    Attributes:
        _gateway_url: OpenShell gateway gRPC endpoint (host:port).
        _cluster: Optional cluster name for from_active_cluster resolution.
    """

    def __init__(
        self,
        gateway_url: str | None = None,
        cluster: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenShell spawner.

        Args:
            gateway_url: OpenShell gateway gRPC endpoint (host:port).
                Falls back to OPENSHELL_GATEWAY_URL env var.
                If neither is set, uses from_active_cluster auto-resolution.
            cluster: Optional cluster name for from_active_cluster.
                Only used when gateway_url is not set.
            **kwargs: Forwarded to AgentSpawner (e.g. max_pods).
        """
        super().__init__(**kwargs)
        self._gateway_url = gateway_url or os.environ.get("OPENSHELL_GATEWAY_URL")
        self._cluster = cluster

    def _get_client(self) -> Any:
        """Create a SandboxClient connected to the OpenShell gateway.

        Returns:
            Connected SandboxClient instance. Caller must close it.
        """
        from openshell import SandboxClient

        if self._gateway_url:
            return SandboxClient(endpoint=self._gateway_url)
        return SandboxClient.from_active_cluster(cluster=self._cluster)

    async def _do_spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str],
        config_override: SpawnConfig | None = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: EphemeralCerts | None = None,
    ) -> str:
        """Create an OpenShell sandbox for the agent.

        Builds a SandboxSpec with the given image and env vars, creates
        the sandbox via the gateway, waits for it to become ready, then
        exposes the HTTP port via ExposeService and returns the URL.

        Args:
            agent_name: Name for the spawned sandbox.
            image: Container image to use.
            env: Environment variables for the sandbox.
            config_override: Optional per-step resource configuration.
            labels: Ignored (OpenShell has no label support on SandboxRef).
            skills_image: Not yet supported; logs a warning.
            skills_paths: Not yet supported.
            service_account: Ignored (OpenShell manages identity).
            read_only: Ignored (OpenShell manages filesystem isolation).
            credential_secret_name: Not supported; logs a warning.
            mcp_secret_mounts: Not supported; logs a warning.
            tls_certs: Logs a warning; OpenShell manages network security.

        Returns:
            HTTP endpoint URL of the sandbox (from ExposeService).

        Raises:
            RuntimeError: If sandbox creation, readiness, or service
                exposure fails.
        """
        from openshell._proto import openshell_pb2

        cfg = config_override or SpawnConfig()
        sandbox_name = f"{SANDBOX_NAME_PREFIX}{agent_name}"

        if tls_certs is not None:
            logger.warning(
                "TLS certs provided for '%s' but OpenShell Gateway manages "
                "network security via its own TLS and network policies. "
                "App-level TLS certs will be ignored. Consider using "
                "SANDBOX_TLS_MODE=mesh when deploying with OpenShell.",
                agent_name,
            )

        if skills_image:
            logger.warning(
                "skills_image '%s' is not yet supported by OpenShellSpawner; "
                "skills must be baked into the sandbox image for now.",
                skills_image,
            )

        if credential_secret_name:
            logger.warning(
                "credential_secret_name '%s' is not supported by OpenShellSpawner; "
                "use OpenShell credential providers instead.",
                credential_secret_name,
            )

        if mcp_secret_mounts:
            logger.warning(
                "mcp_secret_mounts are not supported by OpenShellSpawner; "
                "MCP server credentials must be provided via env vars or "
                "OpenShell credential providers.",
            )

        # Build SandboxSpec with image and environment
        template = openshell_pb2.SandboxTemplate(image=image)
        spec = openshell_pb2.SandboxSpec(
            name=sandbox_name,
            environment=env,
            template=template,
        )

        client = self._get_client()
        try:
            # Create sandbox
            sandbox_ref = await asyncio.to_thread(client.create, spec=spec)
            logger.info(
                "Created OpenShell sandbox '%s' (id=%s) for agent '%s'",
                sandbox_ref.name,
                sandbox_ref.id,
                agent_name,
            )

            # Wait for sandbox to be ready
            try:
                await asyncio.to_thread(
                    client.wait_ready,
                    sandbox_ref.name,
                    timeout_seconds=cfg.timeout_seconds,
                )
            except Exception as exc:
                # Clean up the sandbox if it fails to become ready
                logger.error(
                    "Sandbox '%s' failed to become ready: %s",
                    sandbox_ref.name,
                    exc,
                )
                try:
                    await asyncio.to_thread(client.delete, sandbox_ref.name)
                except Exception:
                    pass
                raise

            # Expose the sandbox HTTP service via the gateway
            target_port = SANDBOX_HTTP_PORT
            expose_request = openshell_pb2.ExposeServiceRequest(
                sandbox=sandbox_ref.name,
                service=SANDBOX_SERVICE_NAME,
                target_port=target_port,
                domain=False,
            )
            try:
                response = await asyncio.to_thread(client._stub.ExposeService, expose_request)
                endpoint = response.url
            except Exception as exc:
                logger.error(
                    "Failed to expose service for sandbox '%s': %s",
                    sandbox_ref.name,
                    exc,
                )
                # Clean up the sandbox if service exposure fails
                try:
                    await asyncio.to_thread(client.delete, sandbox_ref.name)
                except Exception:
                    pass
                raise

            logger.info(
                "OpenShell sandbox '%s' ready at %s",
                sandbox_ref.name,
                endpoint,
            )
            return endpoint
        finally:
            client.close()

    async def _do_destroy(self, agent_name: str) -> None:
        """Delete the OpenShell sandbox.

        Args:
            agent_name: Name of the agent (without the ca-agent- prefix).
        """
        sandbox_name = f"{SANDBOX_NAME_PREFIX}{agent_name}"
        client = self._get_client()
        try:
            await asyncio.to_thread(client.delete, sandbox_name)
            logger.info("Destroyed OpenShell sandbox '%s'", sandbox_name)
        except Exception as exc:
            logger.warning(
                "Failed to destroy OpenShell sandbox '%s': %s",
                sandbox_name,
                exc,
            )
        finally:
            client.close()

    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active OpenShell sandboxes created by this spawner.

        Filters by the ca-agent- naming prefix since SandboxRef does not
        include labels.

        Args:
            labels: Ignored (OpenShell list API has no label filter).
                Included for interface compatibility.

        Returns:
            List of agent names (without the ca-agent- prefix).
        """
        client = self._get_client()
        try:
            all_sandboxes = await asyncio.to_thread(client.list)
            return [
                ref.name.removeprefix(SANDBOX_NAME_PREFIX)
                for ref in all_sandboxes
                if ref.name.startswith(SANDBOX_NAME_PREFIX)
            ]
        except Exception as exc:
            logger.warning("Cannot list OpenShell sandboxes: %s", exc)
            return []
        finally:
            client.close()
