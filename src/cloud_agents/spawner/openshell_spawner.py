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
from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar

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
        driver: str = "podman",
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenShell spawner.

        Args:
            openshell_client: OpenShell SDK client for sandbox operations.
                Must implement create(), wait_ready(), exec(), exec_stream(),
                and delete().
            driver: OpenShell compute driver type ("podman" or "kubernetes").
                Controls how skills_image is handled:
                - "podman": Uses native image mounts via driver_config
                  (no extraction, no tar streaming — Podman mounts the
                  OCI image directly at /app/skills).
                - "kubernetes": Falls back to tar streaming (extract
                  skills locally, stream into sandbox via exec_stream).

        Requires OpenShell gateway v0.0.79+ with gateway_jwt configured.
        The gateway mints sandbox JWTs and delivers them via Podman
        secrets (PR NVIDIA/OpenShell#2156). No client-side JWT workaround
        is needed — the supervisor authenticates directly via gRPC.

        History (issue #82):
            Pre-v0.0.79 gateways used host bind-mounted token files which
            failed on Podman 5.8.x REST API. We had a workaround
            (_inject_podman_token) that extracted JWTs from Podman secrets
            and copied them into containers. This was removed because
            v0.0.79+ fixes the issue upstream and we require v0.0.79+.
        """
        super().__init__(**kwargs)
        self._client = openshell_client
        self._driver = driver
        self._podman_cli: str | None = None
        self._sandbox_names: dict[str, str] = {}
        self._sandbox_ids: dict[str, str] = {}
        self._virtual_hosts: dict[str, str] = {}
        self._server_tasks: dict[str, asyncio.Task] = {}
        self._provider_ids: dict[str, str] = {}

    def get_sandbox_id(self, agent_name: str) -> str | None:
        """Return the sandbox ID (UUID) for an agent, or None if not tracked.

        The OpenShell SDK's exec_stream() requires the sandbox UUID,
        not the human-readable sandbox name.

        Args:
            agent_name: Name of the agent.

        Returns:
            Sandbox ID string (UUID), or None if no sandbox is tracked.
        """
        return self._sandbox_ids.get(agent_name)

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

    async def wait_ready(
        self,
        endpoint: str,
        timeout: float = 60.0,
        health_path: str = "/health",
        ca_cert_pem: bytes | None = None,
    ) -> bool:
        """Skip base readiness check — already done inside _do_spawn.

        OpenShell's _do_spawn performs its own host-aware readiness check
        via _wait_ready_with_host() after ExposeService, so the base
        class wait_ready() call from temporal_activities is redundant.
        """
        return True

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


    _PROVIDER_HOSTS: ClassVar[dict[str, str]] = {
        "openai": "api.openai.com",
        "anthropic": "api.anthropic.com",
        "claude": "api.anthropic.com",
        "gemini": "generativelanguage.googleapis.com",
        "azure": "*.openai.azure.com",
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
                default_port = 443 if parsed.scheme == "https" else 80
                np = spec.policy.network_policies["custom_provider"]
                np.name = "custom-provider"
                ep = np.endpoints.add()
                ep.host = parsed.hostname
                ep.port = parsed.port or default_port
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
                        default_port = 443 if parsed.scheme == "https" else 80
                        np = spec.policy.network_policies[f"mcp_{i}"]
                        np.name = f"mcp-{server.get('name', i)}"
                        ep = np.endpoints.add()
                        ep.host = parsed.hostname
                        ep.port = parsed.port or default_port
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
        skills_image: str | None = None,
        skills_paths: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
        """Create an OpenShell sandbox, start HTTP server, return endpoint."""
        from openshell._proto import openshell_pb2

        if service_account:
            logger.info(
                "OpenShell manages identity — service_account '%s' not applicable",
                service_account,
            )

        if tls_certs is not None:
            logger.info(
                "TLS certs not needed for OpenShell — gateway provides transport security",
            )

        sandbox_name = f"ca-agent-{agent_name}"

        spec = openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(
                image=image,
                labels=labels or {},
            )
        )
        for key, value in env.items():
            spec.environment[key] = value

        self._build_network_policy(spec, env)

        if read_only:
            self._build_filesystem_policy(spec)

        # Skills image: Podman driver mounts the OCI image directly
        # via driver_config (no extraction needed). K8s driver falls
        # back to tar streaming after sandbox creation.
        skills_via_driver_config = False
        if skills_image and self._driver == "podman":
            from google.protobuf import struct_pb2

            mount_config = struct_pb2.Struct()
            mount_config.update({
                "podman": {
                    "mounts": [{
                        "type": "image",
                        "source": skills_image,
                        "target": "/app/skills",
                        "read_only": True,
                    }],
                },
            })
            spec.template.driver_config.MergeFrom(mount_config)
            skills_via_driver_config = True

        sandbox_ref = await asyncio.to_thread(self._client.create, spec=spec)
        sandbox_name = sandbox_ref.name
        sandbox_id = sandbox_ref.id
        self._sandbox_names[agent_name] = sandbox_name
        self._sandbox_ids[agent_name] = sandbox_id

        try:
            await asyncio.to_thread(
                self._client.wait_ready,
                sandbox_name,
                timeout_seconds=300,
            )

            if credential_secret_name:
                await self._inject_credentials(
                    agent_name, sandbox_name, credential_secret_name, env,
                )

            if skills_image and not skills_via_driver_config:
                await self._load_skills(
                    agent_name, sandbox_id, skills_image, skills_paths,
                )

            if mcp_secret_mounts:
                await self._inject_mcp_secrets(agent_name, mcp_secret_mounts, env)

            await self.start_server(sandbox_id, _DEFAULT_SERVER_COMMAND, env=env)

            endpoint, virtual_host = await self._expose_service(
                sandbox_name, port=8080,
            )
            self._virtual_hosts[sandbox_name] = virtual_host

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
            await self._cleanup_sandbox(agent_name, sandbox_name)
            raise

        logger.info(
            "Spawned OpenShell sandbox '%s' (name=%s) at %s",
            agent_name,
            sandbox_name,
            endpoint,
        )
        return endpoint

    async def _cleanup_sandbox(
        self, agent_name: str, sandbox_name: str,
    ) -> None:
        """Clean up a sandbox and all associated resources on failure."""
        provider_id = self._provider_ids.pop(agent_name, None)
        if provider_id:
            try:
                await self._detach_provider(sandbox_name, provider_id)
            except Exception:
                logger.warning(
                    "Failed to detach provider '%s' during cleanup",
                    provider_id, exc_info=True,
                )
        try:
            await asyncio.to_thread(self._client.delete, sandbox_name)
        except Exception:
            logger.warning(
                "Failed to delete orphaned sandbox '%s' during cleanup",
                sandbox_name, exc_info=True,
            )
        self._sandbox_names.pop(agent_name, None)
        self._sandbox_ids.pop(agent_name, None)
        self._virtual_hosts.pop(sandbox_name, None)

    @staticmethod
    def _build_filesystem_policy(spec: "openshell_pb2.SandboxSpec") -> None:
        """Set read-only filesystem policy with write exceptions.

        Allows writes to agent workspace, skills, secrets, and log
        directories so post-create injection still works in advisory mode.
        """
        spec.policy.filesystem.read_only.append("/")
        for rw_path in (
            "/tmp",
            "/home/agent",
            "/var/log",
            "/app/skills",
            "/var/secrets/mcp",
            "/var/run/secrets/llm-credentials",
        ):
            spec.policy.filesystem.read_write.append(rw_path)
        spec.policy.filesystem.include_workdir = True

    async def _inject_credentials(
        self,
        agent_name: str,
        sandbox_name: str,
        credential_secret_name: str,
        env: dict[str, str],
    ) -> None:
        """Inject LLM credentials into the sandbox via Provider API.

        Uses the OpenShell Provider system for gateway-managed credentials.
        Falls back to file-based injection if Provider RPCs fail.
        """
        # credential_secret_name may be K8s-normalized (e.g. "openai-api-key")
        # while the env dict has the original key (e.g. "OPENAI_API_KEY").
        # Try both forms.
        cred_value = env.get(credential_secret_name)
        if not cred_value:
            original_key = credential_secret_name.upper().replace("-", "_")
            cred_value = env.get(original_key)
        if not cred_value:
            raise RuntimeError(
                f"Credential '{credential_secret_name}' not found in env "
                f"for sandbox '{sandbox_name}' — cannot start agent without credentials"
            )

        try:
            provider_id = await self._create_and_attach_provider(
                sandbox_name,
                credentials={credential_secret_name: cred_value},
            )
            self._provider_ids[agent_name] = provider_id
            logger.info(
                "Attached credential provider '%s' to sandbox '%s'",
                provider_id, sandbox_name,
            )
        except Exception:
            logger.warning(
                "Provider API failed for '%s' — falling back to file injection",
                sandbox_name, exc_info=True,
            )
            await self._inject_credentials_via_files(
                agent_name, credential_secret_name, cred_value,
            )

    async def _inject_credentials_via_files(
        self,
        agent_name: str,
        credential_secret_name: str,
        cred_value: str,
    ) -> None:
        """Write credential files to the sandbox filesystem.

        Handles both API-key providers (env var only) and file-backed
        providers like Vertex (GOOGLE_APPLICATION_CREDENTIALS).
        """
        cred_dir = "/var/run/secrets/llm-credentials"
        sandbox_id = self._sandbox_ids[agent_name]
        await self._exec_mkdir(sandbox_id, cred_dir)
        file_path = f"{cred_dir}/{credential_secret_name}"
        await self._do_write_file(agent_name, file_path, cred_value)
        logger.info(
            "Injected credential file '%s' into sandbox for agent '%s'",
            file_path, agent_name,
        )

    async def _create_and_attach_provider(
        self,
        sandbox_name: str,
        credentials: dict[str, str],
    ) -> str:
        """Create an OpenShell provider and attach it to a sandbox.

        Returns the provider ID for later cleanup.
        """
        import grpc
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_provider() -> str:
            grpc_target = self._client._endpoint
            grpc_target = grpc_target.replace("http://", "").replace("https://", "")
            channel = grpc.insecure_channel(grpc_target)
            stub = openshell_pb2_grpc.OpenShellStub(channel)

            create_req = openshell_pb2.CreateProviderRequest(
                provider=openshell_pb2.Provider(
                    type="cloud-agents",
                    credentials=credentials,
                ),
            )
            create_resp = stub.CreateProvider(create_req)
            provider_id = create_resp.provider.id

            attach_req = openshell_pb2.AttachSandboxProviderRequest(
                sandbox=sandbox_name,
                provider=provider_id,
            )
            stub.AttachSandboxProvider(attach_req)
            channel.close()
            return provider_id

        return await asyncio.to_thread(_sync_provider)

    async def _detach_provider(
        self, sandbox_name: str, provider_id: str,
    ) -> None:
        """Detach a provider from a sandbox."""
        import grpc
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_detach() -> None:
            grpc_target = self._client._endpoint
            grpc_target = grpc_target.replace("http://", "").replace("https://", "")
            channel = grpc.insecure_channel(grpc_target)
            stub = openshell_pb2_grpc.OpenShellStub(channel)
            req = openshell_pb2.DetachSandboxProviderRequest(
                sandbox=sandbox_name,
                provider=provider_id,
            )
            stub.DetachSandboxProvider(req)
            channel.close()

        await asyncio.to_thread(_sync_detach)

    async def _load_skills(
        self,
        agent_name: str,
        sandbox_id: str,
        skills_image: str,
        skills_paths: list[str] | None = None,
    ) -> None:
        """Load skills from an OCI image into the sandbox.

        Extracts skills content from the image locally, then streams
        it into the sandbox via exec_stream with tar.

        Cross-platform: uses the Podman Python SDK (available in the
        runner container) to run a transient container that copies
        skills into a temp dir. Falls back to podman CLI if the SDK
        is not available. The sandbox-side tar upload is the same
        regardless of extraction method.
        """
        import shutil
        import tarfile
        from io import BytesIO

        copy_paths = skills_paths or ["/skills"]
        tmp_dir = tempfile.mkdtemp(prefix=f"skills-{agent_name}-")

        try:
            await self._extract_skills_image(
                skills_image, copy_paths, tmp_dir,
            )

            tar_buf = BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                tar.add(tmp_dir, arcname=".")
            tar_bytes = tar_buf.getvalue()

            def _sync_upload() -> None:
                for _ in self._client.exec_stream(
                    sandbox_id,
                    ["tar", "xf", "-", "-C", "/app/skills"],
                    stdin=tar_bytes,
                ):
                    pass

            await asyncio.to_thread(_sync_upload)
            logger.info(
                "Loaded skills into sandbox for agent '%s' from '%s'",
                agent_name, skills_image,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _extract_skills_image(
        self,
        skills_image: str,
        copy_paths: list[str],
        tmp_dir: str,
    ) -> None:
        """Extract skills content from an OCI image to a local directory.

        Fallback chain (first success wins):
        1. crane — static binary, no daemon needed, works everywhere
        2. Podman Python SDK — needs Podman socket
        3. podman CLI — needs binary + socket

        crane is the primary path because it works in K8s pods
        where no container runtime socket is available. Install it
        in the runner Containerfile from go-containerregistry releases.
        """
        import shutil
        import subprocess

        # 1. Try crane (no daemon needed — works in K8s pods)
        crane_bin = shutil.which("crane")
        if crane_bin:
            try:
                def _crane_extract() -> None:
                    result = subprocess.run(
                        [crane_bin, "export", skills_image, "-"],
                        capture_output=True,
                        timeout=120,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"crane export failed: {result.stderr.decode()[:200]}"
                        )
                    import tarfile
                    from io import BytesIO

                    with tarfile.open(fileobj=BytesIO(result.stdout)) as tar:
                        for member in tar:
                            if member.issym() or member.islnk():
                                continue
                            for skill_path in copy_paths:
                                prefix = skill_path.lstrip("/") + "/"
                                if member.name.startswith(prefix):
                                    member.name = member.name[len(prefix):]
                                    resolved = os.path.normpath(
                                        os.path.join(tmp_dir, member.name),
                                    )
                                    if not resolved.startswith(tmp_dir):
                                        continue
                                    tar.extract(member, tmp_dir)

                await asyncio.to_thread(_crane_extract)
                logger.info("Extracted skills via crane from '%s'", skills_image)
                return
            except Exception:
                logger.warning(
                    "crane skills extraction failed, trying Podman SDK",
                    exc_info=True,
                )

        # 2. Try Podman Python SDK (needs socket)
        try:
            from podman import PodmanClient

            def _sdk_extract() -> None:
                pclient = PodmanClient(
                    base_url=f"unix://{os.environ.get('CONTAINER_HOST', '/run/podman/podman.sock').replace('unix://', '')}",
                )
                copy_cmd = " && ".join(
                    f"cp -r {p}/* /out/ 2>/dev/null || true"
                    for p in copy_paths
                )
                pclient.containers.run(
                    skills_image,
                    command=["sh", "-c", copy_cmd],
                    volumes={tmp_dir: {"bind": "/out", "mode": "rw"}},
                    remove=True,
                    detach=False,
                )
                pclient.close()

            await asyncio.to_thread(_sdk_extract)
            logger.info("Extracted skills via Podman SDK from '%s'", skills_image)
            return
        except ImportError:
            pass
        except Exception:
            logger.warning(
                "Podman SDK skills extraction failed, trying CLI",
                exc_info=True,
            )

        # 3. Try podman CLI
        if self._podman_cli:
            for src_path in copy_paths:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        self._podman_cli, "run", "--rm",
                        "-v", f"{tmp_dir}:/out:Z",
                        skills_image,
                        "sh", "-c", f"cp -r {src_path}/* /out/ 2>/dev/null || true",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            logger.info("Extracted skills via podman CLI from '%s'", skills_image)
            return

        raise RuntimeError(
            f"Cannot extract skills image '{skills_image}': "
            "none of crane, Podman SDK, or podman CLI available"
        )

    async def _inject_mcp_secrets(
        self,
        agent_name: str,
        mcp_secret_mounts: list[tuple[str, str, str]],
        env: dict[str, str],
    ) -> None:
        """Inject MCP secret header files into the sandbox.

        Each mount tuple is (secret_name, key, mount_path) where
        mount_path is the directory and key is the filename.
        """
        sandbox_id = self._sandbox_ids.get(agent_name)
        if not sandbox_id:
            raise RuntimeError(f"No sandbox tracked for agent '{agent_name}'")

        for secret_name, key, mount_path in mcp_secret_mounts:
            secret_value = env.get(secret_name, "")
            if not secret_value:
                logger.warning(
                    "MCP secret '%s' not found in env for agent '%s'",
                    secret_name, agent_name,
                )
                continue

            import posixpath

            await self._exec_mkdir(sandbox_id, mount_path)
            file_path = posixpath.join(mount_path, key)
            await self._do_write_file(agent_name, file_path, secret_value)
            logger.info(
                "Injected MCP secret '%s/%s' into sandbox for agent '%s'",
                mount_path, key, agent_name,
            )

    async def _exec_mkdir(self, sandbox_id: str, path: str) -> None:
        """Create a directory inside the sandbox."""
        def _sync_mkdir() -> None:
            for _ in self._client.exec_stream(
                sandbox_id, ["mkdir", "-p", path],
            ):
                pass

        await asyncio.to_thread(_sync_mkdir)

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
        """Delete the OpenShell sandbox and clean up all resources."""
        sandbox_name = self._sandbox_names.get(agent_name)
        if not sandbox_name:
            logger.warning("No sandbox name found for agent '%s'", agent_name)
            return

        task = self._server_tasks.pop(sandbox_name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        provider_id = self._provider_ids.get(agent_name)
        if provider_id:
            try:
                await self._detach_provider(sandbox_name, provider_id)
                self._provider_ids.pop(agent_name, None)
            except Exception:
                logger.warning(
                    "Failed to detach provider '%s' from sandbox '%s' — "
                    "retained for retry on next destroy",
                    provider_id, sandbox_name, exc_info=True,
                )

        try:
            await asyncio.to_thread(self._client.delete, sandbox_name)
            logger.info("Destroyed OpenShell sandbox '%s' (agent=%s)", sandbox_name, agent_name)
        except Exception:
            logger.warning(
                "Failed to destroy sandbox '%s' (agent=%s) — "
                "sandbox retained in _sandbox_names for manual cleanup",
                sandbox_name, agent_name, exc_info=True,
            )
            return
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
