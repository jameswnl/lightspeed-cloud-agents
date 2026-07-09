"""Podman agent spawner — creates Podman containers on demand.

Podman is a supported production deployment target (used by Ansible
and RH Developer Hub teams). Podman socket access grants host-level
container control — deployers should secure the socket appropriately.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any

from cloud_agents.spawner.base import AgentSpawner

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
        self._podman_url = os.environ.get("CONTAINER_HOST") or os.environ.get("DOCKER_HOST")
        self._tls_temp_dirs: dict[str, str] = {}

    def _client(self) -> "PodmanClient":
        """Create a PodmanClient with the configured socket URL."""
        from podman import PodmanClient

        if self._podman_url:
            return PodmanClient(base_url=self._podman_url)
        return PodmanClient()

    async def _do_spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str],
        config: "SpawnConfig | None" = None,
        labels: dict[str, str] | None = None,
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
        """Create a Podman container for the agent."""
        if credential_secret_name:
            logger.warning(
                "K8s Secret volume mount not supported on Podman; "
                "credential_secret_name '%s' will be ignored for '%s'",
                credential_secret_name,
                agent_name,
            )
        if mcp_secret_mounts:
            raise ValueError(
                f"Secret-based MCP headers are not supported on Podman. "
                f"Agent '{agent_name}' requires K8s deployment for MCP secret headers."
            )
        container_name = f"agent-{agent_name}"

        if read_only:
            volumes: dict[str, Any] = {}
            logger.info("Advisory mode: omitting host mounts for '%s'", agent_name)
        else:
            volumes = {
                host: {"bind": ctr, "mode": "ro"} for host, ctr in self._volume_mounts.items()
            }
        skills_volume_name = None

        with self._client() as client:
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
                    is_tls = "8443/tcp" in port_bindings
                    for port_key in ("8443/tcp", "8080/tcp"):
                        for binding in port_bindings.get(port_key, []):
                            host_port = binding.get("HostPort")
                            if host_port:
                                is_tls = port_key == "8443/tcp"
                                break
                        if host_port:
                            break
                    scheme = "https" if is_tls else "http"
                    container_port = 8443 if is_tls else 8080
                    if host_port:
                        return f"{scheme}://localhost:{host_port}"
                    return f"{scheme}://{container_name}:{container_port}"
                existing.remove(force=True)
                logger.info("Removed stale container '%s'", container_name)
            except Exception:
                pass

            container_labels = {"spawned-by": "workflow-runner"}
            if labels:
                container_labels.update(labels)

            # TLS cert injection — write cert+key to temp dir, bind mount
            container_port = 8080
            if tls_certs is not None:
                tls_dir = tempfile.mkdtemp(prefix=f"sandbox-tls-{agent_name}-")
                cert_path = os.path.join(tls_dir, "tls.crt")
                key_path = os.path.join(tls_dir, "tls.key")
                with open(cert_path, "wb") as f:
                    f.write(tls_certs.server_cert_pem)
                with open(key_path, "wb") as f:
                    f.write(tls_certs.server_key_pem)
                os.chmod(cert_path, 0o444)
                os.chmod(key_path, 0o400)
                volumes[tls_dir] = {
                    "bind": "/var/run/secrets/sandbox-tls/",
                    "mode": "ro",
                }
                env["SANDBOX_TLS_CERT_PATH"] = "/var/run/secrets/sandbox-tls/tls.crt"
                env["SANDBOX_TLS_KEY_PATH"] = "/var/run/secrets/sandbox-tls/tls.key"
                container_port = 8443
                self._tls_temp_dirs[agent_name] = tls_dir
                logger.info(
                    "TLS certs injected for '%s' via temp dir '%s'",
                    agent_name,
                    tls_dir,
                )

            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "detach": True,
                "environment": env,
                "network": self._network,
                "network_mode": "bridge",
                "volumes": volumes if volumes else {},
                "ports": {f"{container_port}/tcp": None},
                "labels": container_labels,
                "remove": False,
            }
            if read_only:
                run_kwargs["read_only"] = True
                logger.info(
                    "Advisory mode: running '%s' with read-only filesystem",
                    container_name,
                )

            container = client.containers.run(**run_kwargs)

            container.reload()
            port_bindings = container.ports or {}
            host_port = None
            port_key = f"{container_port}/tcp"
            for binding in port_bindings.get(port_key, []):
                host_port = binding.get("HostPort")
                if host_port:
                    break

        scheme = "https" if tls_certs is not None else "http"
        if self._podman_url:
            endpoint = f"{scheme}://{container_name}:{container_port}"
        elif host_port:
            endpoint = f"{scheme}://localhost:{host_port}"
        else:
            endpoint = f"{scheme}://{container_name}:{container_port}"

        logger.info("Spawned Podman container '%s' at %s", container_name, endpoint)
        return endpoint

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        """Write content to a file inside a Podman container via podman exec.

        Uses stdin piping with ``cat`` to write arbitrary content safely.
        The path is shell-quoted to prevent injection.

        Args:
            agent_name: Name of the agent (without "agent-" prefix).
            path: Absolute file path inside the container.
            content: String content to write.

        Raises:
            RuntimeError: If the write operation fails.
        """
        import shlex
        import subprocess

        container_name = f"agent-{agent_name}"
        try:
            subprocess.run(
                ["podman", "exec", "-i", container_name, "sh", "-c", f"cat > {shlex.quote(path)}"],
                input=content.encode(),
                capture_output=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to write {path} to container {container_name}: {exc.stderr}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Cannot write file to container {container_name}: {exc}"
            ) from exc

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        """Read a file from a Podman container via podman exec cat.

        Args:
            agent_name: Name of the agent (without "agent-" prefix).
            path: Absolute file path inside the container.

        Returns:
            File contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist in the container.
        """
        import subprocess

        container_name = f"agent-{agent_name}"
        try:
            result = subprocess.run(
                ["podman", "exec", container_name, "cat", path],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            if "no such file" in (exc.stderr or "").lower():
                raise FileNotFoundError(f"File not found: {path}") from exc
            raise FileNotFoundError(
                f"Failed to read {path} from container {container_name}: {exc.stderr}"
            ) from exc
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise FileNotFoundError(
                f"Cannot read file from container {container_name}: {exc}"
            ) from exc

    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active Podman containers matching the given labels.

        Args:
            labels: Optional label selector to filter containers.

        Returns:
            List of agent names (without the "agent-" prefix).
        """
        try:
            with self._client() as pc:
                all_filtered: list = []
                for k, v in (labels or {}).items():
                    matches = pc.containers.list(filters={"label": f"{k}={v}"})
                    if not all_filtered:
                        all_filtered = matches
                    else:
                        match_ids = {c.id for c in matches}
                        all_filtered = [c for c in all_filtered if c.id in match_ids]
                return [c.name.removeprefix("agent-") for c in all_filtered]
        except ImportError:
            return []

    async def _do_destroy(self, agent_name: str) -> None:
        """Stop and remove the Podman container."""
        try:
            container_name = f"agent-{agent_name}"
            with self._client() as client:
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

        # Best-effort cleanup of TLS temp directory
        tls_dir = self._tls_temp_dirs.pop(agent_name, None)
        if tls_dir:
            shutil.rmtree(tls_dir, ignore_errors=True)
            logger.info("Cleaned up TLS temp dir '%s' for '%s'", tls_dir, agent_name)
