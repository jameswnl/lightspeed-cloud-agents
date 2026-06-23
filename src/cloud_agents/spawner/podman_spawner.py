"""Podman agent spawner — creates Podman containers for dev/test.

DEV/TEST ONLY. Podman socket access grants host-level container control.
Production uses KubernetesSpawner.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.spawner.base import AgentSpawner

logger = logging.getLogger(__name__)


class PodmanSpawner(AgentSpawner):
    """Spawns Podman containers for on-demand agents. DEV/TEST ONLY.

    Security note: requires Podman socket access, which grants
    host-level container control. Not equivalent to K8s Jobs.

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

    async def _do_spawn(self, agent_name: str, image: str, env: dict[str, str]) -> str:
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

        logger.info("Spawned Podman container '%s' (dev/test only)", container_name)
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
