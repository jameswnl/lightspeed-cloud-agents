"""Podman agent spawner — creates Podman containers on demand.

Podman is a supported production deployment target (used by Ansible
and RH Developer Hub teams). Podman socket access grants host-level
container control — deployers should secure the socket appropriately.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)


class PodmanSpawner(AgentSpawner):
    """Spawns Podman containers for on-demand agents.

    Security note: requires Podman socket access, which grants
    host-level container control. Deployers should restrict socket
    access to authorized users/services.

    Attributes:
        network: Podman network for spawned containers.
    """

    def __init__(self, network: str = "cloud-agents", **kwargs: Any) -> None:
        """Initialize the Podman spawner.

        Args:
            network: Podman network name for container connectivity.
        """
        super().__init__(**kwargs)
        self._network = network

    async def _do_spawn(
        self, agent_name: str, image: str, env: dict[str, str],
        config: "SpawnConfig | None" = None,
    ) -> str:
        """Create a Podman container for the agent."""
        try:
            from podman import PodmanClient
        except ImportError as exc:
            raise RuntimeError("podman-py not installed") from exc

        container_name = f"agent-{agent_name}"

        with PodmanClient() as client:
            container = client.containers.run(
                image=image,
                name=container_name,
                detach=True,
                environment=env,
                network=self._network,
                remove=False,
            )

        logger.info("Spawned Podman container '%s'", container_name)
        return f"http://{container_name}:8080"

    async def _do_destroy(self, agent_name: str) -> None:
        """Stop and remove the Podman container."""
        try:
            from podman import PodmanClient

            container_name = f"agent-{agent_name}"
            with PodmanClient() as client:
                try:
                    container = client.containers.get(container_name)
                    container.stop(timeout=10)
                    container.remove()
                    logger.info("Destroyed Podman container '%s'", container_name)
                except Exception as exc:
                    logger.warning("Failed to destroy container '%s': %s", container_name, exc)
        except ImportError:
            logger.warning("podman-py not installed, cannot destroy container")
