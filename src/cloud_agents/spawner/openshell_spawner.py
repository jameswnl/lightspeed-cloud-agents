"""OpenShell agent spawner — hybrid exec+HTTP communication.

Uses OpenShift sandbox (OpenShell) API to create ephemeral sandboxes,
start HTTP servers via exec, and stream progress events via tail.

Key design: HTTP contract is source of truth for results. The
stream_progress() async generator provides best-effort streaming of
agent work-in-progress events — dropped events are acceptable.

This is OpenShell-specific; other spawners (Podman, K8s) do not
support progress streaming. The caller should check isinstance()
before calling stream_progress().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any, AsyncIterator

from cloud_agents.spawner.base import AgentSpawner

if TYPE_CHECKING:
    from cloud_agents.spawner.base import SpawnConfig
    from cloud_agents.workflow.tls import EphemeralCerts

logger = logging.getLogger(__name__)

# Default command to start the HTTP server inside the sandbox
_DEFAULT_SERVER_COMMAND = [
    "uvicorn",
    "lightspeed_agentic.app:create_app",
    "--host",
    "0.0.0.0",
    "--port",
    "8080",
]

# Path where the sandbox agent writes structured JSONL events
_EVENT_LOG_PATH = "/var/log/agent-events.jsonl"

# Path where the supervisor expects the sandbox JWT (matches
# openshell_core::driver_utils::SANDBOX_TOKEN_MOUNT_PATH in the
# OpenShell Podman driver).
_SANDBOX_TOKEN_PATH = "/etc/openshell/auth/sandbox.jwt"

# Podman secret name prefix for per-sandbox JWTs (matches
# TOKEN_SECRET_PREFIX in openshell-driver-podman container.rs).
_TOKEN_SECRET_PREFIX = "openshell-token-"

# Podman label used by the OpenShell Podman driver to tag containers.
_SANDBOX_ID_LABEL = "openshell.sandbox-id"

# Delay after container restart to allow supervisor to boot.
_POST_RESTART_DELAY_SECS = 3.0


class OpenShellSpawner(AgentSpawner):
    """Spawns sandboxes via OpenShell exec-based communication.

    Hybrid approach: exec to start the HTTP server (fire-and-forget),
    expose the service port for HTTP result contract, and optionally
    stream progress events via tail -f on the event log.

    Attributes:
        _client: OpenShell SDK client instance.
        _sandbox_ids: Map of agent_name -> sandbox_id for cleanup.
        _server_tasks: Map of sandbox_id -> background asyncio.Task
            for the exec'd server process.
    """

    def __init__(
        self,
        openshell_client: Any = None,
        podman_cli: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenShell spawner.

        Args:
            openshell_client: OpenShell SDK client for sandbox operations.
                Must implement create_sandbox(), exec_stream(),
                expose_service(), and delete_sandbox().
            podman_cli: Path to the podman CLI binary. When set, enables
                the Podman secret file mount workaround for Podman 5.8.x
                (issue #82). The workaround extracts the sandbox JWT from
                the Podman secret and copies it into the container after
                creation. Set to None (default) when using K8s or when the
                Podman secret mount works correctly.
        """
        super().__init__(**kwargs)
        self._client = openshell_client
        self._podman_cli = podman_cli
        self._sandbox_ids: dict[str, str] = {}
        self._server_tasks: dict[str, asyncio.Task] = {}

    def get_sandbox_id(self, agent_name: str) -> str | None:
        """Return the sandbox ID for an agent, or None if not tracked.

        Args:
            agent_name: Name of the agent.

        Returns:
            Sandbox ID string, or None if no sandbox is tracked for this agent.
        """
        return self._sandbox_ids.get(agent_name)

    async def _inject_podman_token(self, sandbox_id: str) -> None:
        """Fix Podman 5.8.x secret file mount failure (issue #82).

        The OpenShell Podman driver creates a Podman secret with the
        sandbox JWT and specifies a file mount at the expected path.
        On Podman 5.8.x, the mount is not applied. This workaround:

        1. Finds the container by its openshell.sandbox-id label.
        2. Extracts the JWT from the Podman secret.
        3. Stops the container, copies the token file in, restarts.

        The supervisor reads the token on startup and authenticates
        back to the gateway.

        Args:
            sandbox_id: OpenShell sandbox identifier.

        Raises:
            RuntimeError: If the container or secret is not found, or
                if any podman command fails.
        """
        assert self._podman_cli is not None  # noqa: S101

        # 1. Find the container name by label.
        container_name = await self._podman_find_container(sandbox_id)

        # 2. Extract the JWT from the Podman secret.
        secret_name = f"{_TOKEN_SECRET_PREFIX}{sandbox_id}"
        token = await self._podman_extract_secret(secret_name)

        # 3. Stop container, copy token, restart.
        tmp_path: str | None = None
        try:
            # Write token to temp file for podman cp.
            fd, tmp_path = tempfile.mkstemp(suffix=".jwt", prefix="openshell-token-")
            with os.fdopen(fd, "w") as f:
                f.write(token.strip() + "\n")

            await self._podman_exec(
                "stop",
                "-t",
                "0",
                container_name,
            )

            await self._podman_exec(
                "cp",
                tmp_path,
                f"{container_name}:{_SANDBOX_TOKEN_PATH}",
            )

            await self._podman_exec("start", container_name)

            logger.info(
                "Podman token workaround applied for sandbox '%s' " "(container=%s, secret=%s)",
                sandbox_id,
                container_name,
                secret_name,
            )

            # Brief delay for supervisor to boot with the injected token.
            await asyncio.sleep(_POST_RESTART_DELAY_SECS)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def _podman_find_container(self, sandbox_id: str) -> str:
        """Find the container name for a sandbox by its label.

        Args:
            sandbox_id: OpenShell sandbox identifier.

        Returns:
            Container name string.

        Raises:
            RuntimeError: If no container is found with the expected label.
        """
        assert self._podman_cli is not None  # noqa: S101
        proc = await asyncio.create_subprocess_exec(
            self._podman_cli,
            "ps",
            "-a",
            "--filter",
            f"label={_SANDBOX_ID_LABEL}={sandbox_id}",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        name = stdout.decode().strip()
        if not name:
            raise RuntimeError(
                f"No container found for sandbox '{sandbox_id}' "
                f"(label={_SANDBOX_ID_LABEL}={sandbox_id})"
            )
        # If multiple lines, take the first (shouldn't happen).
        return name.splitlines()[0].strip()

    async def _podman_extract_secret(self, secret_name: str) -> str:
        """Extract a JWT from a Podman secret.

        Args:
            secret_name: Name of the Podman secret.

        Returns:
            The secret data string (JWT).

        Raises:
            RuntimeError: If the secret doesn't exist or can't be read.
        """
        assert self._podman_cli is not None  # noqa: S101
        proc = await asyncio.create_subprocess_exec(
            self._podman_cli,
            "secret",
            "inspect",
            "--showsecret",
            secret_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to extract JWT from Podman secret '{secret_name}': "
                f"{stderr.decode().strip()}"
            )
        try:
            data = json.loads(stdout.decode())
            return data[0]["SecretData"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise RuntimeError(f"Failed to parse Podman secret '{secret_name}': {exc}") from exc

    async def _podman_exec(self, *args: str) -> None:
        """Run a podman CLI command.

        Args:
            *args: Arguments to pass after the podman binary path.

        Raises:
            RuntimeError: If the command exits with a non-zero code.
        """
        assert self._podman_cli is not None  # noqa: S101
        proc = await asyncio.create_subprocess_exec(
            self._podman_cli,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"podman {args[0]} failed (rc={proc.returncode}): " f"{stderr.decode().strip()}"
            )

    async def _do_spawn(
        self,
        agent_name: str,
        image: str,
        env: dict[str, str],
        config: "SpawnConfig | None" = None,
        labels: dict[str, str] | None = None,
        # TODO: Map to OpenShell equivalents for production
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
        """Create an OpenShell sandbox, start HTTP server, return endpoint.

        1. Create sandbox with the given image and env vars.
        2. Exec the HTTP server command (fire-and-forget background task).
        3. Expose the service port and return the routable URL.

        Returns:
            HTTP endpoint URL of the sandbox service.
        """
        sandbox_id = await self._client.create_sandbox(
            image=image,
            env=env,
            labels=labels,
        )
        self._sandbox_ids[agent_name] = sandbox_id

        # Workaround for Podman 5.8.x secret file mount bug (issue #82).
        # The OpenShell Podman driver's secrets field is not applied to
        # the container, so the supervisor can't read the sandbox JWT.
        # Extract the JWT from the Podman secret and copy it in manually.
        if self._podman_cli is not None:
            await self._inject_podman_token(sandbox_id)

        # Start HTTP server via exec (fire-and-forget)
        await self.start_server(sandbox_id, _DEFAULT_SERVER_COMMAND, env=env)

        # Expose service port and get routable endpoint
        endpoint = await self._client.expose_service(sandbox_id, port=8080)

        logger.info(
            "Spawned OpenShell sandbox '%s' (id=%s) at %s",
            agent_name,
            sandbox_id,
            endpoint,
        )
        return endpoint

    async def start_server(
        self,
        sandbox_id: str,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the HTTP server inside a sandbox via exec_stream.

        Fire-and-forget: the exec output is consumed in a background
        asyncio task. The method returns immediately.

        Args:
            sandbox_id: OpenShell sandbox identifier.
            command: Command to execute (e.g. uvicorn invocation).
            env: Optional environment variables for the exec.
        """

        async def _consume_exec() -> None:
            """Consume exec_stream output in background (fire-and-forget)."""
            try:
                async for chunk in self._client.exec_stream(sandbox_id, command, env=env):
                    # Log server output at debug level
                    logger.debug("Server output [%s]: %s", sandbox_id, chunk.rstrip())
            except asyncio.CancelledError:
                logger.info("Server exec cancelled for sandbox '%s'", sandbox_id)
            except Exception:
                logger.warning(
                    "Server exec ended for sandbox '%s'",
                    sandbox_id,
                    exc_info=True,
                )

        task = asyncio.create_task(_consume_exec())
        self._server_tasks[sandbox_id] = task

    async def stream_progress(self, sandbox_id: str) -> AsyncIterator[dict[str, Any]]:
        """Stream agent progress events from the sandbox event log.

        Calls exec_stream with tail -f on the JSONL event log file.
        Yields parsed event dicts as they arrive. Best-effort: connection
        drops are caught and logged, and the generator stops yielding.

        This method is OpenShell-specific. Callers should check
        isinstance(spawner, OpenShellSpawner) before calling.

        Args:
            sandbox_id: OpenShell sandbox identifier.

        Yields:
            Parsed JSONL event dicts from the agent event log.
        """
        tail_cmd = ["tail", "-F", _EVENT_LOG_PATH]
        partial = ""
        try:
            async for chunk in self._client.exec_stream(sandbox_id, tail_cmd):
                data = partial + chunk
                # Split but keep partial last line if chunk doesn't end with newline
                lines = data.split("\n")
                # If data doesn't end with newline, last element is a partial line
                if not data.endswith("\n"):
                    partial = lines[-1]
                    lines = lines[:-1]
                else:
                    partial = ""
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        yield event
                    except json.JSONDecodeError:
                        logger.warning(
                            "Invalid JSON in event stream [%s]: %s",
                            sandbox_id,
                            line[:200],
                        )
        except (ConnectionError, OSError) as exc:
            logger.warning(
                "Progress stream disconnected for sandbox '%s': %s",
                sandbox_id,
                exc,
            )
        except Exception:
            logger.warning(
                "Progress stream error for sandbox '%s'",
                sandbox_id,
                exc_info=True,
            )

    async def _do_write_file(self, agent_name: str, path: str, content: str) -> None:
        """Write content to a file inside an OpenShell sandbox via exec.

        Uses base64 encoding to safely pipe arbitrary content through
        the exec command without shell escaping issues. The path is
        shell-quoted to prevent injection.

        Args:
            agent_name: Name of the agent.
            path: Absolute file path inside the sandbox.
            content: String content to write.

        Raises:
            RuntimeError: If the sandbox is not tracked or write fails.
        """
        import base64
        import shlex

        sandbox_id = self._sandbox_ids.get(agent_name)
        if not sandbox_id:
            raise RuntimeError(f"No sandbox tracked for agent '{agent_name}'")

        encoded = base64.b64encode(content.encode()).decode()
        cmd = ["sh", "-c", f"echo '{encoded}' | base64 -d > {shlex.quote(path)}"]
        try:
            async for _ in self._client.exec_stream(sandbox_id, cmd):
                pass  # consume output
        except Exception as exc:
            raise RuntimeError(f"Failed to write {path} to sandbox {sandbox_id}: {exc}") from exc

    async def _do_read_file(self, agent_name: str, path: str) -> str:
        """Read a file from an OpenShell sandbox via exec.

        Uses exec_stream to run `cat` on the given path inside the sandbox.

        Args:
            agent_name: Name of the agent.
            path: Absolute file path inside the sandbox.

        Returns:
            File contents as a string.

        Raises:
            FileNotFoundError: If the sandbox or file is not found.
        """
        sandbox_id = self._sandbox_ids.get(agent_name)
        if not sandbox_id:
            raise FileNotFoundError(f"No sandbox tracked for agent '{agent_name}'")

        chunks: list[str] = []
        try:
            async for chunk in self._client.exec_stream(sandbox_id, ["cat", path]):
                chunks.append(chunk)
        except Exception as exc:
            if "no such file" in str(exc).lower() or "not found" in str(exc).lower():
                raise FileNotFoundError(f"File not found: {path}") from exc
            raise

        content = "".join(chunks)
        if not content and not chunks:
            raise FileNotFoundError(f"File not found or empty: {path}")
        return content

    async def _do_destroy(self, agent_name: str) -> None:
        """Delete the OpenShell sandbox and clean up background tasks.

        Args:
            agent_name: Name of the agent to destroy.
        """
        sandbox_id = self._sandbox_ids.get(agent_name)
        if not sandbox_id:
            logger.warning("No sandbox ID found for agent '%s'", agent_name)
            return

        # Cancel server background task if running
        task = self._server_tasks.pop(sandbox_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            await self._client.delete_sandbox(sandbox_id)
            logger.info("Destroyed OpenShell sandbox '%s' (agent=%s)", sandbox_id, agent_name)
        except Exception:
            # Do NOT re-raise: base.destroy() always decrements _active_count
            # in its finally block.  Re-raising would cause a double-decrement
            # on retry.  Keep the _sandbox_ids entry so the sandbox is visible
            # via list_active() for manual cleanup or a subsequent destroy().
            logger.warning(
                "Failed to destroy sandbox '%s' (agent=%s) — "
                "sandbox retained in _sandbox_ids for manual cleanup",
                sandbox_id,
                agent_name,
                exc_info=True,
            )
            return
        # Only remove tracking after successful delete
        self._sandbox_ids.pop(agent_name, None)

    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active sandbox agent names.

        Args:
            labels: Optional label filter (not used for in-memory tracking).

        Returns:
            List of agent names with active sandboxes.
        """
        return list(self._sandbox_ids.keys())
