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
from typing import Any, AsyncIterator

from cloud_agents.spawner.base import AgentSpawner

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
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenShell spawner.

        Args:
            openshell_client: OpenShell SDK client for sandbox operations.
                Must implement create_sandbox(), exec_stream(),
                expose_service(), and delete_sandbox().
        """
        super().__init__(**kwargs)
        self._client = openshell_client
        self._sandbox_ids: dict[str, str] = {}
        self._server_tasks: dict[str, asyncio.Task] = {}

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
        try:
            async for chunk in self._client.exec_stream(sandbox_id, tail_cmd):
                for line in chunk.split("\n"):
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

    async def _do_destroy(self, agent_name: str) -> None:
        """Delete the OpenShell sandbox and clean up background tasks.

        Args:
            agent_name: Name of the agent to destroy.
        """
        sandbox_id = self._sandbox_ids.pop(agent_name, None)
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
            logger.warning(
                "Failed to destroy sandbox '%s' (agent=%s)",
                sandbox_id,
                agent_name,
                exc_info=True,
            )

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
