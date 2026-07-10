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

import httpx

from cloud_agents.spawner.base import AgentSpawner

if TYPE_CHECKING:
    from cloud_agents.spawner.base import SpawnConfig
    from cloud_agents.workflow.tls import EphemeralCerts

    from openshell._proto import openshell_pb2

logger = logging.getLogger(__name__)

# Default command to start the HTTP server inside the sandbox
_DEFAULT_SERVER_COMMAND = [
    "uvicorn",
    "lightspeed_agentic.app:app",
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
        _sandbox_names: Map of agent_name -> sandbox_name for cleanup.
        _server_tasks: Map of sandbox_name -> background asyncio.Task
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
                Must implement create(), wait_ready(), exec(), exec_stream(),
                and delete().
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
        self._sandbox_names: dict[str, str] = {}
        self._sandbox_ids: dict[str, str] = {}
        self._virtual_hosts: dict[str, str] = {}
        self._server_tasks: dict[str, asyncio.Task] = {}

    def get_sandbox_id(self, agent_name: str) -> str | None:
        """Return the sandbox name for an agent, or None if not tracked.

        Args:
            agent_name: Name of the agent.

        Returns:
            Sandbox name string, or None if no sandbox is tracked for this agent.
        """
        return self._sandbox_names.get(agent_name)

    def get_sandbox_headers(self, agent_name: str) -> dict[str, str]:
        """Return HTTP headers for gateway-proxied sandbox requests.

        The gateway uses virtual-host routing — requests must include
        a Host header matching the sandbox's exposed service hostname.

        Args:
            agent_name: Name of the agent.

        Returns:
            Dict with Host header, or empty dict if not tracked.
        """
        virtual_host = self._virtual_hosts.get(
            self._sandbox_names.get(agent_name, ""), ""
        )
        if virtual_host:
            return {"Host": virtual_host}
        return {}

    async def _expose_service(
        self,
        sandbox_name: str,
        port: int = 8080,
    ) -> tuple[str, str]:
        """Expose a sandbox port via the gateway's HTTP proxy.

        Calls the ExposeService gRPC method on the gateway. Returns
        a gateway-routable endpoint and the virtual hostname for
        Host-header-based routing.

        Args:
            sandbox_name: OpenShell sandbox name.
            port: Target port inside the sandbox.

        Returns:
            Tuple of (gateway_endpoint_url, virtual_hostname).
        """
        import grpc
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_expose() -> tuple[str, str]:
            # Strip http:// scheme — gRPC channels use bare host:port
            grpc_target = self._client._endpoint
            grpc_target = grpc_target.replace("http://", "").replace("https://", "")

            channel = grpc.insecure_channel(grpc_target)
            stub = openshell_pb2_grpc.OpenShellStub(channel)
            req = openshell_pb2.ExposeServiceRequest(
                sandbox=sandbox_name,
                target_port=port,
            )
            resp = stub.ExposeService(req)
            channel.close()

            from urllib.parse import urlparse

            parsed = urlparse(resp.url)
            virtual_host = parsed.hostname or ""
            rewritten_url = f"http://{grpc_target}"
            return rewritten_url, virtual_host

        return await asyncio.to_thread(_sync_expose)

    async def _wait_ready_with_host(
        self,
        endpoint: str,
        virtual_host: str,
        timeout: float = 60.0,
        health_path: str = "/health",
    ) -> bool:
        """Wait for sandbox readiness via gateway-proxied health check.

        Parallel-safe: takes the virtual host as a parameter rather
        than reading shared instance state.

        Args:
            endpoint: Gateway HTTP endpoint URL.
            virtual_host: Virtual hostname for Host header routing.
            timeout: Maximum wait time in seconds.
            health_path: Health check path.

        Returns:
            True if the sandbox became ready, False if timed out.
        """
        import time

        headers = {"Host": virtual_host} if virtual_host else {}
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{endpoint}{health_path}",
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
        return False

    async def _inject_podman_token(self, sandbox_name: str) -> None:
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
            sandbox_name: OpenShell sandbox name.

        Raises:
            RuntimeError: If the container or secret is not found, or
                if any podman command fails.
        """
        assert self._podman_cli is not None  # noqa: S101

        # 1. Find the container name by label.
        container_name = await self._podman_find_container(sandbox_name)

        # 2. Extract the JWT from the Podman secret.
        secret_name = f"{_TOKEN_SECRET_PREFIX}{sandbox_name}"
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
                sandbox_name,
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

    async def _podman_find_container(self, sandbox_name: str) -> str:
        """Find the container name for a sandbox by its label.

        Args:
            sandbox_name: OpenShell sandbox name.

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
            f"label={_SANDBOX_ID_LABEL}={sandbox_name}",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        name = stdout.decode().strip()
        if not name:
            raise RuntimeError(
                f"No container found for sandbox '{sandbox_name}' "
                f"(label={_SANDBOX_ID_LABEL}={sandbox_name})"
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

    _PROVIDER_HOSTS: dict[str, str] = {
        "openai": "api.openai.com",
        "anthropic": "api.anthropic.com",
        "claude": "api.anthropic.com",
        "gemini": "generativelanguage.googleapis.com",
        "azure_openai": "*.openai.azure.com",
    }

    @staticmethod
    def _build_network_policy(
        spec: "openshell_pb2.SandboxSpec",
        env: dict[str, str],
    ) -> None:
        """Derive sandbox network policy from step environment config.

        Automatically allows egress to the LLM provider and any
        configured MCP servers. Workflow authors don't need to write
        OpenShell policy YAML — the spawner derives it from existing
        provider and MCP config.

        Args:
            spec: SandboxSpec to populate with network_policies.
            env: Environment variables for the step (contains provider
                name, provider URL, and MCP server config).
        """
        spec.policy.version = 1

        # LLM provider egress
        provider = env.get("LIGHTSPEED_PROVIDER", "")
        provider_host = OpenShellSpawner._PROVIDER_HOSTS.get(provider)
        if provider_host:
            np = spec.policy.network_policies["llm_provider"]
            np.name = "llm-provider"
            ep = np.endpoints.add()
            ep.host = provider_host
            ep.port = 443
            b = np.binaries.add()
            b.path = "**"

        # Custom provider URL egress
        provider_url = env.get("LIGHTSPEED_PROVIDER_URL", "")
        if provider_url:
            from urllib.parse import urlparse

            parsed = urlparse(provider_url)
            if parsed.hostname:
                np = spec.policy.network_policies["custom_provider"]
                np.name = "custom-provider"
                ep = np.endpoints.add()
                ep.host = parsed.hostname
                ep.port = parsed.port or 443
                b = np.binaries.add()
                b.path = "**"

        # MCP server egress (parsed from LIGHTSPEED_MCP_SERVERS JSON)
        mcp_json = env.get("LIGHTSPEED_MCP_SERVERS", "")
        if mcp_json:
            try:
                mcp_servers = json.loads(mcp_json)
                for i, server in enumerate(mcp_servers):
                    url = server.get("url", "")
                    if not url:
                        continue
                    from urllib.parse import urlparse

                    parsed = urlparse(url)
                    if parsed.hostname:
                        np = spec.policy.network_policies[f"mcp_{i}"]
                        np.name = f"mcp-{server.get('name', i)}"
                        ep = np.endpoints.add()
                        ep.host = parsed.hostname
                        ep.port = parsed.port or 443
                        b = np.binaries.add()
                        b.path = "**"
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse LIGHTSPEED_MCP_SERVERS for network policy")

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
        2. Wait for sandbox to be ready.
        3. Exec the HTTP server command (fire-and-forget background task).
        4. Return the network-local endpoint URL.

        Returns:
            HTTP endpoint URL of the sandbox service.
        """
        from openshell._proto import openshell_pb2

        # Build sandbox name using our naming convention
        sandbox_name = f"ca-agent-{agent_name}"

        # Construct SandboxSpec for the new API
        spec = openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(
                image=image,
                labels=labels or {},
            )
        )
        # Add environment variables
        for key, value in env.items():
            spec.environment[key] = value

        # Derive network policy from step config (provider, MCP servers)
        self._build_network_policy(spec, env)

        # Create sandbox (sync call wrapped in thread)
        sandbox_ref = await asyncio.to_thread(self._client.create, spec=spec)
        sandbox_name = sandbox_ref.name
        sandbox_id = sandbox_ref.id
        self._sandbox_names[agent_name] = sandbox_name
        self._sandbox_ids[agent_name] = sandbox_id

        try:
            # Wait for sandbox to be ready
            await asyncio.to_thread(
                self._client.wait_ready,
                sandbox_name,
                timeout_seconds=300,
            )

            # Workaround for Podman 5.8.x secret file mount bug (issue #82).
            if self._podman_cli is not None:
                await self._inject_podman_token(sandbox_name)

            # Start HTTP server via exec (fire-and-forget).
            # exec_stream takes sandbox_id (UUID), not sandbox_name.
            await self.start_server(sandbox_id, _DEFAULT_SERVER_COMMAND, env=env)

            # Expose the sandbox port via the gateway's HTTP proxy.
            endpoint, virtual_host = await self._expose_service(
                sandbox_name, port=8080,
            )
            self._virtual_hosts[sandbox_name] = virtual_host

            # Wait for the sandbox HTTP server to be ready.
            # Done here (not in base wait_ready) so each parallel
            # spawn uses its own virtual host — no shared state race.
            ready = await self._wait_ready_with_host(
                endpoint, virtual_host, timeout=60.0,
            )
            if not ready:
                raise RuntimeError(
                    f"Sandbox '{sandbox_name}' HTTP server did not become ready"
                )
        except Exception:
            logger.warning(
                "Post-create step failed for sandbox '%s' (agent=%s); "
                "deleting sandbox to prevent orphan",
                sandbox_name,
                agent_name,
                exc_info=True,
            )
            try:
                await asyncio.to_thread(self._client.delete, sandbox_name)
            except Exception:
                logger.warning(
                    "Failed to delete orphaned sandbox '%s' during cleanup",
                    sandbox_name,
                    exc_info=True,
                )
            self._sandbox_names.pop(agent_name, None)
            self._sandbox_ids.pop(agent_name, None)
            self._virtual_hosts.pop(sandbox_name, None)
            raise

        logger.info(
            "Spawned OpenShell sandbox '%s' (name=%s) at %s",
            agent_name,
            sandbox_name,
            endpoint,
        )
        return endpoint

    async def start_server(
        self,
        sandbox_name: str,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the HTTP server inside a sandbox via exec_stream.

        Fire-and-forget: the exec output is consumed in a background
        asyncio task. The method returns immediately.

        Args:
            sandbox_name: OpenShell sandbox name.
            command: Command to execute (e.g. uvicorn invocation).
            env: Optional environment variables for the exec.
        """

        async def _consume_exec() -> None:
            """Consume exec_stream output in background (fire-and-forget)."""
            try:
                # exec_stream is now a sync iterator — wrap in to_thread
                def _sync_consume():
                    for item in self._client.exec_stream(sandbox_name, command, env=env):
                        # item is ExecChunk or ExecResult
                        if hasattr(item, "chunk"):
                            chunk = item.chunk
                        else:
                            # ExecResult — log final status
                            logger.debug("Server exec ended [%s]: %s", sandbox_name, item)
                            continue
                        logger.debug("Server output [%s]: %s", sandbox_name, chunk.rstrip())

                await asyncio.to_thread(_sync_consume)
            except asyncio.CancelledError:
                logger.info("Server exec cancelled for sandbox '%s'", sandbox_name)
            except Exception:
                logger.warning(
                    "Server exec ended for sandbox '%s'",
                    sandbox_name,
                    exc_info=True,
                )

        task = asyncio.create_task(_consume_exec())
        self._server_tasks[sandbox_name] = task

    async def stream_progress(self, sandbox_name: str) -> AsyncIterator[dict[str, Any]]:
        """Stream agent progress events from the sandbox event log.

        Calls exec_stream with tail -f on the JSONL event log file.
        Yields parsed event dicts as they arrive. Best-effort: connection
        drops are caught and logged, and the generator stops yielding.

        This method is OpenShell-specific. Callers should check
        isinstance(spawner, OpenShellSpawner) before calling.

        Args:
            sandbox_name: OpenShell sandbox name.

        Yields:
            Parsed JSONL event dicts from the agent event log.
        """
        tail_cmd = ["tail", "-F", _EVENT_LOG_PATH]
        partial = ""

        # Use a queue to communicate between thread and async code
        import queue
        q: queue.Queue = queue.Queue()

        def _sync_stream():
            """Run in thread - consume sync iterator and put events in queue."""
            nonlocal partial
            try:
                for item in self._client.exec_stream(sandbox_name, tail_cmd):
                    # item is ExecChunk or ExecResult
                    if hasattr(item, "chunk"):
                        chunk = item.chunk
                    else:
                        # ExecResult — stream ended
                        break

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
                            q.put(("event", event))
                        except json.JSONDecodeError:
                            logger.warning(
                                "Invalid JSON in event stream [%s]: %s",
                                sandbox_name,
                                line[:200],
                            )
            except (ConnectionError, OSError) as exc:
                q.put(("error", exc))
            except Exception as exc:
                q.put(("error", exc))
            finally:
                q.put(("done", None))

        # Start thread to consume sync iterator
        import threading
        thread = threading.Thread(target=_sync_stream, daemon=True)
        thread.start()

        try:
            while True:
                # Poll queue with timeout to allow async event loop to run
                try:
                    msg_type, msg_data = await asyncio.to_thread(q.get, timeout=0.1)
                except queue.Empty:
                    continue

                if msg_type == "event":
                    yield msg_data
                elif msg_type == "error":
                    if isinstance(msg_data, (ConnectionError, OSError)):
                        logger.warning(
                            "Progress stream disconnected for sandbox '%s': %s",
                            sandbox_name,
                            msg_data,
                        )
                    else:
                        logger.warning(
                            "Progress stream error for sandbox '%s'",
                            sandbox_name,
                            exc_info=True,
                        )
                    break
                elif msg_type == "done":
                    break
        finally:
            # Wait for thread to finish
            thread.join(timeout=1.0)

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
            def _sync_exec():
                for _ in self._client.exec_stream(sandbox_id, cmd):
                    pass

            await asyncio.to_thread(_sync_exec)
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
            def _sync_exec():
                for item in self._client.exec_stream(sandbox_id, ["cat", path]):
                    if hasattr(item, "chunk"):
                        chunks.append(item.chunk)

            await asyncio.to_thread(_sync_exec)
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
        sandbox_name = self._sandbox_names.get(agent_name)
        if not sandbox_name:
            logger.warning("No sandbox name found for agent '%s'", agent_name)
            return

        # Cancel server background task if running
        task = self._server_tasks.pop(sandbox_name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            await asyncio.to_thread(self._client.delete, sandbox_name)
            logger.info("Destroyed OpenShell sandbox '%s' (agent=%s)", sandbox_name, agent_name)
        except Exception:
            # Do NOT re-raise: base.destroy() always decrements _active_count
            # in its finally block.  Re-raising would cause a double-decrement
            # on retry.  Keep the _sandbox_names entry so the sandbox is visible
            # via list_active() for manual cleanup or a subsequent destroy().
            logger.warning(
                "Failed to destroy sandbox '%s' (agent=%s) — "
                "sandbox retained in _sandbox_names for manual cleanup",
                sandbox_name,
                agent_name,
                exc_info=True,
            )
            return
        # Only remove tracking after successful delete
        sandbox_name = self._sandbox_names.pop(agent_name, None)
        self._sandbox_ids.pop(agent_name, None)
        if sandbox_name:
            self._virtual_hosts.pop(sandbox_name, None)

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
        return list(self._sandbox_names.keys())
