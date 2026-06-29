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

    def __init__(
        self,
        network: str = "cloud-agents",
        volume_mounts: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Podman spawner.

        Args:
            network: Podman network name for container connectivity.
            volume_mounts: Host path → container path mappings for agent config/tools.
        """
        super().__init__(**kwargs)
        self._network = network
        self._volume_mounts = volume_mounts or {}

    async def _do_spawn(
        self, agent_name: str, image: str, env: dict[str, str],
        config: "SpawnConfig | None" = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
    ) -> str:
        """Create a Podman container for the agent."""
        try:
            from podman import PodmanClient
        except ImportError as exc:
            raise RuntimeError("podman-py not installed") from exc

        container_name = f"agent-{agent_name}"

        if read_only:
            volumes: dict[str, Any] = {}
            logger.info("Advisory mode: omitting host mounts for '%s'", agent_name)
        else:
            volumes = {host: {"bind": ctr, "mode": "ro"} for host, ctr in self._volume_mounts.items()}
        skills_volume_name = None

        with PodmanClient() as client:
            if skills_image:
                skills_volume_name = f"skills-{agent_name}"
                try:
                    client.volumes.create({"Name": skills_volume_name})
                except Exception:
                    pass
                copy_paths = skills_paths or ["/skills"]
                copy_cmd = " && ".join(f"cp -r {p} /skills-data/" for p in copy_paths)
                client.containers.run(
                    skills_image,
                    command=["sh", "-c", copy_cmd],
                    volumes={skills_volume_name: {"bind": "/skills-data", "mode": "rw"}},
                    remove=True,
                    detach=False,
                )
                volumes[skills_volume_name] = {"bind": "/app/skills", "mode": "ro"}
            try:
                existing = client.containers.get(container_name)
                if existing.status == "running":
                    logger.info("Container '%s' already running (idempotent)", container_name)
                    existing.reload()
                    port_bindings = existing.ports or {}
                    host_port = None
                    for binding in port_bindings.get("8080/tcp", []):
                        host_port = binding.get("HostPort")
                        if host_port:
                            break
                    if host_port:
                        return f"http://localhost:{host_port}"
                    return f"http://{container_name}:8080"
                existing.remove(force=True)
                logger.info("Removed stale container '%s'", container_name)
            except Exception:
                pass

            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "detach": True,
                "environment": env,
                "network": self._network,
                "volumes": volumes or None,
                "ports": {"8080/tcp": None},
                "labels": labels or {},
                "remove": False,
            }
            if read_only:
                run_kwargs["read_only"] = True
                logger.info("Advisory mode: running '%s' with read-only filesystem", container_name)

            container = client.containers.run(**run_kwargs)

            container.reload()
            port_bindings = container.ports or {}
            host_port = None
            for binding in port_bindings.get("8080/tcp", []):
                host_port = binding.get("HostPort")
                if host_port:
                    break

        if host_port:
            endpoint = f"http://localhost:{host_port}"
        else:
            endpoint = f"http://{container_name}:8080"

        logger.info("Spawned Podman container '%s' at %s", container_name, endpoint)
        return endpoint

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
                try:
                    skills_vol = client.volumes.get(f"skills-{agent_name}")
                    skills_vol.remove()
                    logger.info("Removed skills volume 'skills-%s'", agent_name)
                except Exception:
                    pass
        except ImportError:
            logger.warning("podman-py not installed, cannot destroy container")
