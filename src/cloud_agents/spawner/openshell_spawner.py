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
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar

import httpx

from cloud_agents.spawner.base import AgentSpawner

if TYPE_CHECKING:
    from cloud_agents.spawner.base import SpawnConfig
    from cloud_agents.workflow.security.tls import EphemeralCerts

    from openshell._proto import openshell_pb2


logger = logging.getLogger(__name__)

# Default command to start the HTTP server inside the sandbox.
# Invoked as `python3 -m uvicorn` rather than the bare `uvicorn` binary:
# images that install Python deps with `pip install --target` (a common
# hermetic-build pattern) copy package files but never generate
# console-script executables, so a bare "uvicorn" exec fails with
# "command not found" even though the package is importable.
_DEFAULT_SERVER_COMMAND = [
    "python3",
    "-m",
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

    # Extra read-only paths granted on top of OpenShell's own hardcoded
    # default allowlist for every non-advisory spawn (see
    # _build_baseline_filesystem_policy()). These are where the reference
    # sandbox image (quay.io/jameswong/lightspeed-agentic-sandbox) installs
    # Python packages and application code -- neither is in OpenShell's
    # restrictive_default_policy(), so without this every non-advisory
    # spawn fails with Landlock EACCES on its own dependencies (issue #189).
    _DEFAULT_EXTRA_READABLE_PATHS: ClassVar[list[str]] = ["/opt/app-root", "/opt/lightspeed"]

    # Extra env vars merged into the exec'd server process's environment
    # for every spawn (see _do_spawn()'s call to start_server()).
    # OpenShell's supervisor calls env_clear() before exec'ing a command via
    # exec()/exec_stream() (ssh.rs apply_child_env()), then rebuilds the
    # environment from a hardcoded allowlist plus OPENSHELL_USER_ENVIRONMENT
    # (the caller-supplied env= passed to exec_stream() -- that part does
    # reach the child). What's lost is the sandbox *image's* own `ENV
    # PYTHONPATH=...` from its Containerfile: env_clear() wipes it and
    # PYTHONPATH isn't in the supervisor's allowlist, so a plain `env=` with
    # no PYTHONPATH key silently omits it -- unlike a normal `exec()` on the
    # host, where a missing key would just inherit the parent's value. The
    # reference sandbox image (quay.io/jameswong/lightspeed-agentic-sandbox,
    # production Containerfile) installs its own lightspeed_agentic module
    # at /opt/lightspeed/src, outside the interpreter's default
    # site-packages, so it always needs PYTHONPATH explicitly -- without
    # this, every non-advisory spawn fails with "HTTP server did not become
    # ready" (issue #192). Value copied verbatim from that Containerfile's
    # `ENV PYTHONPATH=...` line; re-verify against the image source if this
    # ever needs to change (e.g. a Python version bump).
    #
    # Where materialize-skills.sh copies the allowed_skills subset so
    # providers can list it (see _materialize_allowed_skills()). Must be
    # granted Landlock read_write in _build_baseline_filesystem_policy()
    # when allowed_skills is set -- /app itself is read-only in the
    # baseline policy, and Landlock denies writes regardless of the
    # image's own POSIX chmod/chown, so omitting this grant makes
    # materialize-skills.sh fail with EACCES even though the sandbox
    # user nominally owns the directory (reproduced against a real
    # gateway; POSIX permissions alone are not sufficient).
    _MATERIALIZED_SKILLS_DIR: ClassVar[str] = "/app/skills"

    # LIGHTSPEED_SKILLS_DIR is set for the same env_clear() reason: the
    # image's own `ENV LIGHTSPEED_SKILLS_DIR=/app/skills` declaration is
    # wiped before the exec'd server process starts. Providers must list
    # /app/skills specifically -- the materialize-skills.sh output
    # (_do_spawn() execs it before start_server() when allowed_skills is
    # set) -- not the read-only /skills master, which holds every baked-in
    # skill regardless of allowed_skills (issue #202).
    _DEFAULT_EXTRA_ENV: ClassVar[dict[str, str]] = {
        "PYTHONPATH": "/opt/lightspeed/src:/opt/app-root/lib64/python3.12/site-packages",
        "LIGHTSPEED_SKILLS_DIR": _MATERIALIZED_SKILLS_DIR,
    }

    # OpenShell's own hardcoded restrictive_default_policy() (Rust side,
    # crates/openshell-policy/src/lib.rs, confirmed against openshell@679fe4c).
    # Mirrored here because OpenShell does NOT merge a supplied filesystem
    # policy with its own default -- a supplied policy fully *replaces* it.
    # So the baseline builder below must always send this full list,
    # union'd with the extra paths, never just the extras alone -- see
    # issue #189. If this list ever drifts from OpenShell's own, re-verify
    # against restrictive_default_policy() directly rather than trusting
    # this comment.
    _DEFAULT_BASELINE_READ_ONLY: ClassVar[list[str]] = [
        "/usr",
        "/lib",
        "/proc",
        "/dev/urandom",
        "/app",
        "/etc",
        "/var/log",
    ]
    _DEFAULT_BASELINE_READ_WRITE: ClassVar[list[str]] = ["/tmp", "/dev/null"]

    # All available skills are baked into the sandbox image at build time
    # under this path (see lightspeed-agentic-sandbox's Containerfile) --
    # outside /app so this spawner's per-skill Landlock grants (see
    # allowed_skills in _build_baseline_filesystem_policy()) never have to
    # narrow /app's own baseline grant (needed for Claude Code's own
    # node_modules/CLI symlink). Nothing writes here at runtime; the old
    # skills_image/skills_paths runtime mount-and-copy mechanism (issue
    # #202) is gone.
    _SKILLS_ROOT: ClassVar[str] = "/skills"

    # CreateSandboxRequest.name is left empty (gateway auto-generates a
    # short, unique, routable name) -- the gateway enforces a 19-character
    # limit on caller-supplied names (three DNS-1123 segments must fit a
    # 63-char label: 19 + 2 + 19 + 2 + 19 = 61), which real agent_name
    # values (e.g. "ca-<12 hex chars>") already approach on their own and
    # would exceed with any prefix. So agent_name is recovered via a durable
    # label instead of the sandbox name (issue #224).
    _AGENT_NAME_LABEL_KEY: ClassVar[str] = "cloud-agents/agent-name"

    # Attached to every sandbox at create time (merged with caller-supplied
    # labels) so reconcile_orphaned_sandboxes() can find sandboxes spawned by
    # a *previous* process instance via ListSandboxes(label_selector=...)
    # (issue #224). Must match the label reconcile_orphaned_sandboxes() in
    # workflow/executor/temporal/entrypoint.py filters on.
    _SPAWNED_BY_LABEL: ClassVar[dict[str, str]] = {"spawned-by": "workflow-runner"}

    # Baked into the sandbox image (lightspeed-agentic-sandbox's
    # Containerfile); copies the allowed_skills subset from _SKILLS_ROOT
    # into LIGHTSPEED_SKILLS_DIR so providers' directory-listing-based
    # skill discovery sees only the scoped set. See
    # _materialize_allowed_skills().
    _MATERIALIZE_SKILLS_SCRIPT: ClassVar[str] = "/usr/local/bin/materialize-skills.sh"

    @staticmethod
    def _validate_extra_readable_paths(paths: list[str]) -> list[str]:
        """Validate extra_readable_paths before they reach a Landlock policy.

        Extra readable paths *widen* the sandbox's filesystem access, so
        malformed input here is a security concern, not just a usability
        one. Rejects relative paths (meaningless to Landlock, which rules
        operate on absolute filesystem paths), ".." segments (could escape
        the intended directory), empty strings, and "/" itself (would grant
        full-filesystem read on the baseline policy without advisory
        mode's write lockdown -- effectively disabling the read
        restriction this fix exists to preserve).

        Args:
            paths: Candidate list of absolute directory paths.

        Returns:
            A new list with the same entries, if all are valid. A copy is
            returned (rather than the input list itself) so a caller
            mutating the list they passed in can't retroactively change
            the spawner's stored policy.

        Raises:
            ValueError: If any path is empty, relative, "/", or contains
                a ".." segment.
        """
        for path in paths:
            if not path:
                raise ValueError("extra_readable_paths entries must not be empty")
            if not path.startswith("/"):
                raise ValueError(f"extra_readable_paths entries must be absolute paths: {path!r}")
            if ".." in path.split("/"):
                raise ValueError(
                    f"extra_readable_paths entries must not contain '..' segments: {path!r}"
                )
            # Both checks are needed, neither alone is sufficient:
            # - strip("/") == "" catches "/", "//", "///" -- but NOT "/."
            #   or "/././", since strip() doesn't resolve "." segments.
            # - normpath() resolves "/." and "/././" to "/" -- but leaves
            #   "//" as "//" unchanged (POSIX special-cases exactly two
            #   leading slashes), so it alone would miss "//".
            if path.strip("/") == "" or os.path.normpath(path) in ("/", "//"):
                raise ValueError(
                    "extra_readable_paths entries must not be the filesystem root "
                    f"('/') -- this would grant full-filesystem read: {path!r}"
                )
        return list(paths)

    @staticmethod
    def _validate_extra_env(env: dict[str, str]) -> dict[str, str]:
        """Validate extra_env before it's merged into an exec'd process's environment.

        Args:
            env: Candidate env var name -> value mapping.

        Returns:
            A new dict with the same entries, if all are valid. A copy is
            returned (rather than the input dict itself) so a caller
            mutating the dict they passed in can't retroactively change
            the spawner's stored defaults.

        Raises:
            ValueError: If any key is empty.
        """
        for key in env:
            if not key:
                raise ValueError("extra_env keys must not be empty")
        return dict(env)

    @staticmethod
    def _validate_allowed_skills(names: list[str]) -> None:
        """Validate allowed_skills before building /skills/<name> Landlock paths.

        Entries are meant to be bare directory names (e.g. "k8s-diag"),
        not paths -- allowing "/" or ".." segments would let a
        request-supplied name escape _SKILLS_ROOT via naive string
        joining (issue #202).

        Args:
            names: Candidate skill names.

        Raises:
            ValueError: If any name is empty or contains a "/" or ".."
                segment.
        """
        for name in names:
            if not name:
                raise ValueError("allowed_skills entries must not be empty")
            if "/" in name:
                raise ValueError(f"allowed_skills entries must not contain '/': {name!r}")
            if name in (".", ".."):
                raise ValueError(f"allowed_skills entries must not be '.' or '..': {name!r}")

    def __init__(
        self,
        openshell_client: Any = None,
        workspace: str = "default",
        endpoint: str = "",
        http_endpoint: str = "",
        tls_ca: str = "",
        tls_cert: str = "",
        tls_key: str = "",
        bearer_token: str = "",
        bearer_token_provider: Callable[[], str] | None = None,
        extra_readable_paths: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the OpenShell spawner.

        Args:
            openshell_client: OpenShell SDK client for sandbox operations.
                Must implement create(), wait_ready(), exec(), exec_stream(),
                and delete().
            endpoint: Gateway gRPC endpoint (host:port). Used for raw
                gRPC calls (ExposeService, Provider API). Falls back to
                reading client._endpoint if empty.
            http_endpoint: Override for the HTTP proxy endpoint returned
                by ExposeService. When set, this URL is used instead of
                the gateway-returned resp.url. Useful when the gateway's
                internal URL is not routable from the runner.
            tls_ca: Path to CA certificate for gRPC TLS channels.
            tls_cert: Path to client certificate for mTLS.
            tls_key: Path to client key for mTLS.
            bearer_token: OIDC bearer token for gRPC auth. Applied to
                raw gRPC channels via call credentials interceptor.
            bearer_token_provider: Optional zero-arg callable returning a
                fresh bearer token string. Mutually exclusive with
                bearer_token. _create_grpc_channel() builds a new gRPC
                channel on every call (not once at construction time), so
                calling this provider there re-reads the current token on
                every RPC -- letting a caller supply its own refreshing
                token strategy (e.g. an OIDC client-credentials provider
                with its own caching) without OpenShellSpawner needing to
                know about OIDC/Keycloak specifics. Fixes short-lived
                bearer tokens expiring mid-spawn with no way to refresh
                (issue #236).
            extra_readable_paths: Additional absolute paths to grant
                read-only Landlock access to, on top of OpenShell's own
                default allowlist, for every non-advisory spawn. Defaults
                to ["/opt/app-root", "/opt/lightspeed"] -- where the
                reference sandbox image installs Python packages and
                application code. A derived image with a different
                layout should override this. See
                _build_baseline_filesystem_policy() and issue #189.
            extra_env: Additional environment variables merged into the
                exec'd server process's environment for every spawn.
                Defaults to the reference sandbox image's PYTHONPATH
                (see _DEFAULT_EXTRA_ENV). A derived image with a
                different layout, or no need for this at all, should
                override this (pass {} to disable). See issue #192.
        """
        super().__init__(**kwargs)
        self._client = openshell_client
        self._workspace = workspace
        self._endpoint = endpoint
        self._http_endpoint = http_endpoint
        self._tls_ca = tls_ca
        self._tls_cert = tls_cert
        self._tls_key = tls_key
        self._bearer_token = bearer_token
        if bearer_token and bearer_token_provider is not None:
            raise ValueError(
                "bearer_token and bearer_token_provider are mutually exclusive -- "
                "pass one or the other, not both"
            )
        self._bearer_token_provider = bearer_token_provider
        self._extra_readable_paths = self._validate_extra_readable_paths(
            extra_readable_paths
            if extra_readable_paths is not None
            else list(self._DEFAULT_EXTRA_READABLE_PATHS)
        )
        self._extra_env = self._validate_extra_env(
            extra_env if extra_env is not None else dict(self._DEFAULT_EXTRA_ENV)
        )
        self._sandbox_names: dict[str, str] = {}
        self._sandbox_ids: dict[str, str] = {}
        self._virtual_hosts: dict[str, str] = {}
        self._server_tasks: dict[str, asyncio.Task] = {}
        # Despite the name, stores each provider's *name* (metadata.name),
        # not its metadata.id -- see _create_provider()'s docstring.
        self._provider_ids: dict[str, str] = {}

    def _resolve_grpc_target(self) -> str:
        """Return the bare host:port gRPC target for raw channel creation."""
        target = self._endpoint or getattr(self._client, "_endpoint", "")
        return target.replace("http://", "").replace("https://", "")

    @staticmethod
    def _read_file(path: str) -> bytes:
        """Read a file and return its contents as bytes."""
        with open(path, "rb") as f:
            return f.read()

    def _create_grpc_channel(self) -> Any:
        """Create a gRPC channel with TLS and bearer token auth.

        Returns an insecure channel when no TLS is configured, or a
        secure channel with optional mTLS client certs and/or bearer
        token via composite_channel_credentials.

        Raises ValueError if a bearer token (static or provider-sourced) is
        set without TLS — sending OIDC tokens over plaintext is a
        credential leak.

        Called fresh, inline, on every raw gRPC call (ExposeService,
        Provider API) rather than once at spawner-construction time --
        so when bearer_token_provider is set, this re-invokes it on every
        call, picking up a refreshed token instead of reusing one that may
        have expired mid-request (issue #236).
        """
        import grpc

        target = self._resolve_grpc_target()

        token = (
            self._bearer_token_provider()
            if self._bearer_token_provider is not None
            else self._bearer_token
        )

        if self._bearer_token_provider is not None and not token:
            raise ValueError(
                "bearer_token_provider returned an empty token -- refusing to "
                "build an unauthenticated gRPC channel. A configured provider "
                "is a strong signal that auth was intended, so an empty "
                "result is treated as a provider failure, not as 'no auth "
                "configured'."
            )

        if token and not self._tls_ca:
            raise ValueError(
                "OPENSHELL_BEARER_TOKEN or bearer_token_provider requires "
                "TLS (OPENSHELL_TLS_CA). Refusing to send credentials over "
                "plaintext."
            )

        if not self._tls_ca:
            return grpc.insecure_channel(target)

        root_certs = self._read_file(self._tls_ca)
        private_key = self._read_file(self._tls_key) if self._tls_key else None
        cert_chain = self._read_file(self._tls_cert) if self._tls_cert else None
        channel_creds = grpc.ssl_channel_credentials(
            root_certificates=root_certs,
            private_key=private_key,
            certificate_chain=cert_chain,
        )

        if token:
            call_creds = grpc.access_token_call_credentials(token)
            channel_creds = grpc.composite_channel_credentials(channel_creds, call_creds)

        return grpc.secure_channel(target, channel_creds)

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
        virtual_host = self._virtual_hosts.get(self._sandbox_names.get(agent_name, ""), "")
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
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_expose() -> tuple[str, str]:
            channel = self._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)
                req = openshell_pb2.ExposeServiceRequest(
                    sandbox=sandbox_name,
                    target_port=port,
                )
                resp = stub.ExposeService(req)
            finally:
                channel.close()

            from urllib.parse import urlparse

            parsed = urlparse(resp.url)
            virtual_host = parsed.hostname or ""

            if self._http_endpoint:
                endpoint_url = self._http_endpoint.rstrip("/")
            else:
                # Default to the gRPC endpoint for HTTP proxy access.
                # The gateway multiplexes gRPC and HTTP on the same port;
                # virtual-host routing uses the Host header (set by caller).
                # resp.url contains a *.openshell.localhost hostname that
                # only resolves in environments with wildcard DNS.
                target = self._resolve_grpc_target()
                scheme = "https" if self._tls_ca else "http"
                endpoint_url = f"{scheme}://{target}"

            return endpoint_url, virtual_host

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

    def get_query_ssl_context(self) -> ssl.SSLContext | None:
        """Return the SSL context callers should use for HTTPS calls to
        sandboxes this spawner exposes (e.g. step_runner.py's query call
        to the sandbox's exposed endpoint).

        Without this, a caller has no way to learn that this spawner's
        exposed endpoints are served behind the OpenShell gateway's own
        (often self-signed) TLS cert -- step_runner.py's query-time HTTP
        client used to fall back to httpx's default system trust store,
        which fails with CERTIFICATE_VERIFY_FAILED against a real
        deployment's self-signed gateway CA (issue #194). Mirrors the same
        construction _wait_ready_with_host() uses internally for its own
        readiness check, factored out so both share one implementation.

        Returns:
            An ssl.SSLContext built from this spawner's tls_ca/tls_cert/
            tls_key, or None if no tls_ca is configured (this spawner's
            endpoints are plain HTTP -- see _expose_service()'s scheme
            selection -- so there's nothing to verify).
        """
        if not self._tls_ca:
            return None
        ssl_ctx = ssl.create_default_context(cafile=self._tls_ca)
        if self._tls_cert and self._tls_key:
            ssl_ctx.load_cert_chain(self._tls_cert, self._tls_key)
        return ssl_ctx

    # Consecutive bare-404 responses (no body, no content-type) before
    # _wait_ready_with_host() gives up early and raises a diagnostic error
    # instead of waiting out the full timeout (issue #209). Chosen high
    # enough (~10s+ of continuous signal at the 2s poll interval) that a
    # transient blip while a route propagates on a slower-but-working
    # gateway (real OCP, local Kind) shouldn't false-positive.
    _INGRESS_MISMATCH_STREAK_LIMIT: ClassVar[int] = 5

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

        Raises:
            RuntimeError: If the gateway returns a bare 404 (no body, no
                content-type) several times in a row -- this is the
                fingerprint of an ingress that doesn't support
                Host-header-based HTTP routing to sandbox ports on its
                main TLS port (issue #209), not a slow-starting sandbox.
                Raised early instead of waiting out the full timeout so
                the failure is diagnosable rather than a generic
                "did not become ready" after 60s.
        """
        import time

        headers = {"Host": virtual_host} if virtual_host else {}
        verify: bool | ssl.SSLContext | None = self.get_query_ssl_context()
        if verify is None:
            verify = True

        ingress_mismatch_streak = 0

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                async with httpx.AsyncClient(timeout=5.0, verify=verify) as client:
                    resp = await client.get(
                        f"{endpoint}{health_path}",
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        return True
                    if (
                        resp.status_code == 404
                        and not resp.content
                        and "content-type" not in resp.headers
                    ):
                        ingress_mismatch_streak += 1
                        if ingress_mismatch_streak >= self._INGRESS_MISMATCH_STREAK_LIMIT:
                            raise RuntimeError(
                                f"Gateway returned a bare 404 (no body, no content-type) "
                                f"{ingress_mismatch_streak} times in a row for Host "
                                f"'{virtual_host}' at {endpoint}{health_path}. This "
                                "matches the fingerprint of an ingress that doesn't "
                                "support Host-header-based HTTP routing to sandbox "
                                "ports on its main TLS port (see issue #209) -- the "
                                "sandbox's app itself may be perfectly healthy. If this "
                                "gateway exposes a separate HTTP ingress route for "
                                "sandboxes, construct this spawner with "
                                "http_endpoint=<that route's URL> instead."
                            )
                    else:
                        ingress_mismatch_streak = 0
            except httpx.HTTPError:
                ingress_mismatch_streak = 0
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

        LIGHTSPEED_PROVIDER's default host and LIGHTSPEED_PROVIDER_URL are
        mutually exclusive: when both are set, only LIGHTSPEED_PROVIDER_URL's
        host gets an egress rule, so sandboxes routed through a custom
        inference proxy don't also get direct egress to the vendor's public
        API (issue #209).

        Args:
            spec: SandboxSpec to populate with network_policies.
            env: Environment variables for the step (contains provider
                name, provider URL, and MCP server config).
        """
        spec.policy.version = 1

        # LLM provider egress. Skipped when LIGHTSPEED_PROVIDER_URL is also
        # set -- the two are mutually exclusive routing modes (either talk to
        # the vendor's default public host, or talk to a configured override
        # such as a gateway-internal inference proxy), not additive. Adding
        # both would grant sandboxes direct internet egress to the vendor's
        # API even when the deployment intends to route exclusively through
        # the custom URL (issue #209).
        provider_url = env.get("LIGHTSPEED_PROVIDER_URL", "")
        provider = env.get("LIGHTSPEED_PROVIDER", "")
        provider_host = OpenShellSpawner._PROVIDER_HOSTS.get(provider)
        if provider_host and not provider_url:
            np = spec.policy.network_policies["llm_provider"]
            np.name = "llm-provider"
            ep = np.endpoints.add()
            ep.host = provider_host
            ep.port = 443
            b = np.binaries.add()
            b.path = "**"

        # Custom provider URL egress
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
            else:
                logger.warning(
                    "LIGHTSPEED_PROVIDER_URL=%r has no parseable hostname -- "
                    "no custom_provider egress rule added, and the default "
                    "%s egress rule is also suppressed since the URL is set. "
                    "Sandbox will have no LLM provider network egress.",
                    provider_url,
                    provider or "provider",
                )

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
        allowed_skills: list[str] | None = None,
        service_account: str | None = None,
        read_only: bool = False,
        credential_secret_name: str | None = None,
        mcp_secret_mounts: list[tuple[str, str, str]] | None = None,
        tls_certs: "EphemeralCerts | None" = None,
    ) -> str:
        """Create an OpenShell sandbox, start HTTP server, return endpoint."""
        from openshell._proto import openshell_pb2

        if skills_image or skills_paths:
            logger.warning(
                "skills_image/skills_paths requested for agent '%s' but OpenShellSpawner no "
                "longer supports them -- skills now ship baked into the sandbox image at "
                "%s, scoped per-run via allowed_skills instead (issue #202). Ignoring.",
                agent_name,
                self._SKILLS_ROOT,
            )

        if service_account:
            logger.info(
                "OpenShell manages identity — service_account '%s' not applicable",
                service_account,
            )

        if tls_certs is not None:
            logger.info(
                "TLS certs not needed for OpenShell — gateway provides transport security",
            )

        # Placeholder for log messages only, until overwritten by the
        # gateway-assigned sandbox_ref.name below -- CreateSandboxRequest.name
        # is left empty so the gateway generates the real (short, routable)
        # name (issue #224; see _AGENT_NAME_LABEL_KEY).
        sandbox_name = f"ca-agent-{agent_name}"

        # --- Credential handling (issue #199): create Provider BEFORE sandbox creation ---
        # The real credential must never be placed in spec.environment; OpenShell's
        # Provider system will inject a placeholder (openshell:resolve:env:...)
        # via spec.providers and resolve it only inside the supervisor's proxy.
        # We create the Provider here, before the sandbox exists, and attach
        # via spec.providers at create time. The credential value is fetched
        # from the runner's own environment (not from the caller's env dict,
        # which no longer contains it after step_runner fix) or from env dict
        # as fallback for tests that pass it explicitly.
        provider_id: str | None = None
        if credential_secret_name:
            # Resolve credential value: try env dict first (for tests), then os.environ
            original_key = credential_secret_name.upper().replace("-", "_")
            cred_value = (
                env.get(credential_secret_name)
                or env.get(original_key)
                or os.environ.get(original_key)
                or os.environ.get(credential_secret_name)
            )
            if not cred_value:
                raise RuntimeError(
                    f"Credential '{credential_secret_name}' not found in env "
                    f"for sandbox '{sandbox_name}' — cannot start agent without credentials"
                )
            # Do not let the real credential leak into spec.environment even
            # if the caller mistakenly included it in env dict
            # (defense in depth for pre-fix callers)
            # Filter it out when populating spec.environment below
            try:
                # Use the uppercased env var name as the credential key
                # (e.g., OPENAI_API_KEY) since the agent reads that env var
                # and the placeholder is openshell:resolve:env:OPENAI_API_KEY
                env_cred_key = credential_secret_name.upper().replace("-", "_")
                provider_id = await self._create_provider(
                    credentials={env_cred_key: cred_value},
                )
                self._provider_ids[agent_name] = provider_id
                logger.info(
                    "Created credential provider '%s' for sandbox '%s'",
                    provider_id,
                    sandbox_name,
                )
            except Exception:
                logger.warning(
                    "Provider creation failed for '%s' — failing spawn (no file fallback)",
                    sandbox_name,
                    exc_info=True,
                )
                raise

        # Merge caller labels with the fixed spawned-by label and the
        # agent_name recovery label; both always win over caller-supplied
        # labels of the same key so a caller can't accidentally suppress
        # orphan discovery (issue #224).
        sandbox_labels = {
            **(labels or {}),
            **self._SPAWNED_BY_LABEL,
            self._AGENT_NAME_LABEL_KEY: agent_name,
        }

        spec = openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(
                image=image,
                labels=labels or {},
            )
        )
        # Filter credential keys from environment to avoid direct exposure
        cred_keys = set()
        if credential_secret_name:
            cred_keys.add(credential_secret_name)
            cred_keys.add(credential_secret_name.upper().replace("-", "_"))
        for key, value in env.items():
            if key in cred_keys:
                continue
            spec.environment[key] = value
        if provider_id:
            spec.providers.append(provider_id)

        self._build_network_policy(spec, env)

        if read_only:
            self._build_filesystem_policy(spec)
        else:
            self._build_baseline_filesystem_policy(spec, allowed_skills=allowed_skills)

        try:
            sandbox_ref = await asyncio.to_thread(
                self._client.create,
                workspace=self._workspace,
                spec=spec,
                labels=sandbox_labels,
            )
            sandbox_name = sandbox_ref.name
            sandbox_id = sandbox_ref.id
            self._sandbox_names[agent_name] = sandbox_name
            self._sandbox_ids[agent_name] = sandbox_id

            await asyncio.to_thread(
                self._client.wait_ready,
                sandbox_name,
                workspace=self._workspace,
                timeout_seconds=300,
            )

            # Credential Provider already created and attached via spec.providers
            # before sandbox creation (see above) -- no post-create injection needed.
            # The old _inject_credentials path (CreateProvider + Attach after
            # wait_ready with real env) is removed to avoid exposing the real
            # credential in spec.environment (issue #199).

            if mcp_secret_mounts:
                await self._inject_mcp_secrets(agent_name, mcp_secret_mounts, env)

            # Advisory (read_only=True) spawns use _build_filesystem_policy(),
            # which grants blanket "/" read but no write outside /tmp,
            # /home/agent, /var/log, MCP, and credentials -- not
            # _MATERIALIZED_SKILLS_DIR. Materializing there would fail with
            # the same EACCES this method now raises loudly for, so advisory
            # spawns skip materialize entirely and list _SKILLS_ROOT (the
            # master) directly below instead -- consistent with advisory
            # mode's existing documented semantics that its blanket read
            # grant already covers all of /skills regardless of
            # allowed_skills, since advisory is an integrity boundary
            # (no-write), not a confidentiality one.
            if allowed_skills and not read_only:
                await self._materialize_allowed_skills(sandbox_id, allowed_skills)

            # self._extra_env first, then the caller's own env on top, so an
            # explicit caller-provided value (e.g. a different derived image's
            # PYTHONPATH) wins on collision rather than being silently
            # overridden by the spawner's own default (issue #192).
            # Filter credential keys from server_env as well -- start_server
            # execs inside the sandbox, so env there would also expose the
            # real credential to the sandboxed process (issue #199).
            filtered_env = {k: v for k, v in env.items() if k not in cred_keys}
            server_env = {**self._extra_env, **filtered_env}
            if read_only:
                server_env["LIGHTSPEED_SKILLS_DIR"] = self._SKILLS_ROOT
            await self.start_server(sandbox_id, _DEFAULT_SERVER_COMMAND, env=server_env)

            endpoint, virtual_host = await self._expose_service(
                sandbox_name,
                port=8080,
            )
            self._virtual_hosts[sandbox_name] = virtual_host

            ready = await self._wait_ready_with_host(
                endpoint,
                virtual_host,
                timeout=60.0,
            )
            if not ready:
                raise RuntimeError(f"Sandbox '{sandbox_name}' HTTP server did not become ready")
        except Exception:
            logger.warning(
                "Post-create step failed for sandbox '%s' (agent=%s); "
                "deleting sandbox to prevent orphan",
                sandbox_name,
                agent_name,
                exc_info=True,
            )
            # _cleanup_sandbox() detaches the provider from the sandbox (if
            # still tracked) and deletes the sandbox. This must happen
            # BEFORE deleting the provider itself -- the gateway refuses
            # DeleteProvider while it's still attached to a sandbox
            # (FAILED_PRECONDITION), which previously leaked the provider
            # on every post-create failure (issue #214).
            await self._cleanup_sandbox(agent_name, sandbox_name)
            if provider_id:
                try:
                    await self._delete_provider(provider_id)
                    logger.info(
                        "Cleaned up orphaned provider '%s' after failed create", provider_id
                    )
                except Exception:
                    logger.warning(
                        "Failed to delete orphaned provider '%s'", provider_id, exc_info=True
                    )
            raise

        logger.info(
            "Spawned OpenShell sandbox '%s' (name=%s) at %s",
            agent_name,
            sandbox_name,
            endpoint,
        )
        return endpoint

    async def _cleanup_sandbox(
        self,
        agent_name: str,
        sandbox_name: str,
    ) -> None:
        """Clean up a sandbox and all associated resources on failure."""
        provider_id = self._provider_ids.pop(agent_name, None)
        if provider_id:
            try:
                await self._detach_provider(sandbox_name, provider_id)
            except Exception:
                logger.warning(
                    "Failed to detach provider '%s' during cleanup",
                    provider_id,
                    exc_info=True,
                )
        try:
            await asyncio.to_thread(self._client.delete, sandbox_name, workspace=self._workspace)
        except Exception:
            logger.warning(
                "Failed to delete orphaned sandbox '%s' during cleanup",
                sandbox_name,
                exc_info=True,
            )
        self._sandbox_names.pop(agent_name, None)
        self._sandbox_ids.pop(agent_name, None)
        self._virtual_hosts.pop(sandbox_name, None)

    @staticmethod
    def _build_filesystem_policy(spec: "openshell_pb2.SandboxSpec") -> None:
        """Set read-only filesystem policy with write exceptions.

        Allows writes to agent workspace, secrets, and log directories so
        post-create injection still works in advisory mode.

        Note: this grants "/" read-only, which already includes all of
        _SKILLS_ROOT regardless of allowed_skills -- advisory mode's own
        design (let the agent read everything to investigate, write
        nothing) means per-skill Landlock scoping only has effect in the
        non-advisory baseline policy below, not here.
        """
        spec.policy.filesystem.read_only.append("/")
        for rw_path in (
            "/tmp",
            "/home/agent",
            "/var/log",
            "/var/secrets/mcp",
            "/var/run/secrets/llm-credentials",
        ):
            spec.policy.filesystem.read_write.append(rw_path)
        spec.policy.filesystem.include_workdir = True

    def _build_baseline_filesystem_policy(
        self, spec: "openshell_pb2.SandboxSpec", allowed_skills: list[str] | None = None
    ) -> None:
        """Set the baseline filesystem policy sent for every non-advisory spawn.

        Without this, a non-advisory spawn sends an empty
        `spec.policy.filesystem`, and OpenShell's gateway falls back to
        its own hardcoded `restrictive_default_policy()` -- which never
        includes /opt/app-root or /opt/lightspeed, where this sandbox
        image's Python packages and application code live. That causes
        Landlock to deny the sandbox's own process access to its own
        dependencies (e.g. uvicorn), failing every non-advisory ephemeral
        run on real OpenShift clusters (issue #189).

        This is additive, not a replacement for _build_filesystem_policy()
        (the advisory/full-lockdown path) -- the two are mutually
        exclusive per spawn and this method must never run alongside it.

        Critically, OpenShell does NOT merge a supplied filesystem policy
        with its own default -- a supplied policy fully *replaces* it. So
        this sends the complete default read_only/read_write allowlist
        (mirrored in _DEFAULT_BASELINE_READ_ONLY/_DEFAULT_BASELINE_READ_WRITE)
        union'd with self._extra_readable_paths, never just the extra
        paths alone -- sending only the extras would drop /usr, /lib,
        /proc, /etc, and /tmp, breaking things worse than the bug this
        fixes.

        allowed_skills, if set, grants read-only access to the specific
        _SKILLS_ROOT/<name> subdirectories named -- all available skills
        are baked into the sandbox image at build time (see
        lightspeed-agentic-sandbox), read-only content nothing writes to
        at runtime, so this is a read grant, not the write grant the old
        skills_image tar-upload mechanism needed (issue #202). OpenShell's
        read_only access right already includes execute
        (AccessFs::from_read() = Execute | ReadFile | ReadDir in the
        landlock crate OpenShell depends on, confirmed against its
        source), so skill scripts remain runnable with no separate grant.
        None or omitted means no skills are visible -- least-privilege
        default, not "all".
        """
        for path in self._DEFAULT_BASELINE_READ_ONLY:
            spec.policy.filesystem.read_only.append(path)
        for path in self._extra_readable_paths:
            spec.policy.filesystem.read_only.append(path)
        for path in self._DEFAULT_BASELINE_READ_WRITE:
            spec.policy.filesystem.read_write.append(path)
        if allowed_skills:
            self._validate_allowed_skills(allowed_skills)
            for skill_name in allowed_skills:
                spec.policy.filesystem.read_only.append(f"{self._SKILLS_ROOT}/{skill_name}")
            # materialize-skills.sh needs to write here; /app itself is
            # only read-only above.
            spec.policy.filesystem.read_write.append(self._MATERIALIZED_SKILLS_DIR)
        spec.policy.filesystem.include_workdir = True
        # Already the proto default, but set explicitly to match the
        # live-verified fix YAML in issue #189 exactly.
        spec.policy.landlock.compatibility = "best_effort"

    async def _inject_credentials(
        self,
        agent_name: str,
        sandbox_name: str,
        credential_secret_name: str,
        env: dict[str, str],
    ) -> None:
        """Inject LLM credentials into the sandbox via Provider API.

        .. deprecated:: This post-create path is deprecated. Credentials
            are now injected via spec.providers at sandbox creation time
            (see _do_spawn). This method is retained for backwards
            compatibility in tests but should not be used for new code.
            It no longer falls back to file injection with real values.
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
                provider_id,
                sandbox_name,
            )
        except Exception:
            logger.error(
                "Provider API failed for '%s' — not falling back to file injection "
                "(file fallback would expose real credential; failing spawn)",
                sandbox_name,
                exc_info=True,
            )
            raise

    async def _inject_credentials_via_files(
        self,
        agent_name: str,
        credential_secret_name: str,
        cred_value: str,
    ) -> None:
        """Write credential files to the sandbox filesystem.

        .. deprecated:: This method would write the raw credential value
            directly into a sandbox-readable file, exposing it to the
            sandboxed process (issue #199). It is retained for backwards
            compatibility but now raises instead of writing.
        """
        raise RuntimeError(
            "_inject_credentials_via_files is deprecated -- credentials are now "
            "injected via Provider placeholder, not via files with real values"
        )

    async def _create_provider(
        self,
        credentials: dict[str, str],
    ) -> str:
        """Create an OpenShell provider and return its name.

        Returns metadata.name, not metadata.id -- spec.providers,
        AttachSandboxProvider, and DetachSandboxProvider all resolve
        providers by name, not id (confirmed against a real gateway;
        passing the id gets "provider '<id>' not found"). Called
        "provider_id" at most call sites for historical reasons; it's
        actually the provider's name.

        For use before sandbox creation -- the provider is attached via
        SandboxSpec.providers at create time, not via a separate Attach
        call. The caller is responsible for storing the name for cleanup.

        Requires TLS (tls_ca) to avoid sending credentials over cleartext
        gRPC (issue #199 review). In-cluster service URL with disable_tls
        is not considered secure for credential transmission.
        """
        if not self._tls_ca:
            import os as _os

            # Fail-closed by default: credentials must not be sent over
            # cleartext gRPC. Opt *in* to insecure for local Kind via
            # OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 (not the reverse).
            if _os.environ.get("OPENSHELL_ALLOW_INSECURE_CREDENTIALS") != "1":
                raise ValueError(
                    "Provider creation requires TLS (OPENSHELL_TLS_CA) -- "
                    "refusing to send credentials over insecure channel. "
                    "Set OPENSHELL_TLS_CA for TLS, or "
                    "OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 for local Kind (insecure)."
                )
            logger.warning(
                "Creating provider without TLS (OPENSHELL_TLS_CA not set) -- "
                "credential will be sent over insecure channel (OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)."
            )
        from openshell._proto import datamodel_pb2, openshell_pb2, openshell_pb2_grpc

        def _sync_create() -> str:
            channel = self._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)
                create_req = openshell_pb2.CreateProviderRequest(
                    workspace=self._workspace,
                    provider=datamodel_pb2.Provider(
                        type="cloud-agents",
                        credentials=credentials,
                    ),
                )
                create_resp = stub.CreateProvider(create_req)
                # spec.providers/AttachSandboxProvider/DetachSandboxProvider all
                # resolve by the provider's *name* (see provider_name/name fields
                # below), not its id -- confirmed against a real gateway that
                # passing metadata.id here makes CreateSandbox fail with
                # "provider '<id>' not found" even though the provider exists.
                name = create_resp.provider.metadata.name
                if not name:
                    raise RuntimeError(
                        "Gateway returned an empty provider name from CreateProvider "
                        "-- cannot reference this provider from spec.providers"
                    )
                return name
            finally:
                channel.close()

        return await asyncio.to_thread(_sync_create)

    async def _create_and_attach_provider(
        self,
        sandbox_name: str,
        credentials: dict[str, str],
    ) -> str:
        """Create an OpenShell provider and attach it to a sandbox.

        .. deprecated:: Use _create_provider with spec.providers instead.
            This post-create attach path is retained for backwards
            compatibility but now also requires TLS.

        Returns the provider's name for later cleanup (see the matching
        note in _create_provider() -- called "id" at most call sites for
        historical reasons; it's actually the name).
        """
        if not self._tls_ca:
            import os as _os

            # Fail-closed by default: credentials must not be sent over
            # cleartext gRPC. Opt *in* to insecure for local Kind via
            # OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 (not the reverse).
            if _os.environ.get("OPENSHELL_ALLOW_INSECURE_CREDENTIALS") != "1":
                raise ValueError(
                    "Provider creation requires TLS (OPENSHELL_TLS_CA) -- "
                    "refusing to send credentials over insecure channel. "
                    "Set OPENSHELL_TLS_CA for TLS, or "
                    "OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1 for local Kind (insecure)."
                )
            logger.warning(
                "Creating provider without TLS (OPENSHELL_TLS_CA not set) -- "
                "credential will be sent over insecure channel (OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1)."
            )
        from openshell._proto import datamodel_pb2, openshell_pb2, openshell_pb2_grpc

        def _sync_provider() -> str:
            channel = self._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)

                create_req = openshell_pb2.CreateProviderRequest(
                    workspace=self._workspace,
                    provider=datamodel_pb2.Provider(
                        type="cloud-agents",
                        credentials=credentials,
                    ),
                )
                create_resp = stub.CreateProvider(create_req)
                # See the matching comment in _create_provider() -- attach/detach
                # resolve providers by name, not id.
                provider_id = create_resp.provider.metadata.name
                if not provider_id:
                    raise RuntimeError(
                        "Gateway returned an empty provider name from CreateProvider "
                        "-- cannot attach this provider to the sandbox"
                    )

                attach_req = openshell_pb2.AttachSandboxProviderRequest(
                    sandbox_name=sandbox_name,
                    provider_name=provider_id,
                    workspace=self._workspace,
                )
                stub.AttachSandboxProvider(attach_req)
                return provider_id
            finally:
                channel.close()

        return await asyncio.to_thread(_sync_provider)

    async def _delete_provider(
        self,
        provider_id: str,
    ) -> None:
        """Delete a provider (for cleanup of orphaned providers)."""
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_delete() -> None:
            channel = self._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)
                req = openshell_pb2.DeleteProviderRequest(
                    name=provider_id,
                    workspace=self._workspace,
                )
                stub.DeleteProvider(req)
            finally:
                channel.close()

        await asyncio.to_thread(_sync_delete)

    async def _detach_provider(
        self,
        sandbox_name: str,
        provider_id: str,
    ) -> None:
        """Detach a provider from a sandbox."""
        from openshell._proto import openshell_pb2, openshell_pb2_grpc

        def _sync_detach() -> None:
            channel = self._create_grpc_channel()
            try:
                stub = openshell_pb2_grpc.OpenShellStub(channel)
                req = openshell_pb2.DetachSandboxProviderRequest(
                    sandbox_name=sandbox_name,
                    provider_name=provider_id,
                    workspace=self._workspace,
                )
                stub.DetachSandboxProvider(req)
            finally:
                channel.close()

        await asyncio.to_thread(_sync_detach)

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
                    secret_name,
                    agent_name,
                )
                continue

            import posixpath

            await self._exec_mkdir(sandbox_id, mount_path)
            file_path = posixpath.join(mount_path, key)
            await self._do_write_file(agent_name, file_path, secret_value)
            logger.info(
                "Injected MCP secret '%s/%s' into sandbox for agent '%s'",
                mount_path,
                key,
                agent_name,
            )

    async def _exec_mkdir(self, sandbox_id: str, path: str) -> None:
        """Create a directory inside the sandbox."""

        def _sync_mkdir() -> None:
            for _ in self._client.exec_stream(
                sandbox_id,
                ["mkdir", "-p", path],
            ):
                pass

        await asyncio.to_thread(_sync_mkdir)

    async def _materialize_allowed_skills(self, sandbox_id: str, allowed_skills: list[str]) -> None:
        """Copy the allowed_skills subset into the provider-listable skills dir.

        Providers discover skills by *listing* LIGHTSPEED_SKILLS_DIR, and
        Landlock's allow-list model can't grant partial listing of
        _SKILLS_ROOT without granting full listing (which would defeat
        per-name scoping) -- see _build_baseline_filesystem_policy(). So
        instead this execs the sandbox image's baked-in
        materialize-skills.sh (lightspeed-agentic-sandbox), which copies
        just these names from _SKILLS_ROOT into a plain, freshly-listable
        directory. The real enforcement remains the per-name Landlock
        grant on _SKILLS_ROOT/<name>: an unlisted name is unreadable to
        this copy too, not merely absent from it.

        Validated independently here (not only in
        _build_baseline_filesystem_policy()) since this is the sole
        exec-reaching call site for allowed_skills -- advisory-mode
        spawns never call this method at all (see _do_spawn(), which
        skips materialize entirely when read_only=True), so this
        validation is defense in depth against a future non-advisory
        call path rather than a gap advisory mode would otherwise hit.

        Raises:
            RuntimeError: If materialize-skills.sh exits nonzero (e.g. the
                Landlock write grant is missing, or an older image predates
                the script entirely -- ENOENT/127). exec_stream() does not
                raise on a nonzero exit by itself, so without this check a
                failed materialize silently leaves the sandbox with no (or
                partial) skills while spawn() reports success -- exactly
                the failure this method exists to prevent, reproduced
                live against a real gateway before this check was added.
        """
        self._validate_allowed_skills(allowed_skills)

        def _sync_materialize() -> Any:
            result = None
            for item in self._client.exec_stream(
                sandbox_id,
                [self._MATERIALIZE_SKILLS_SCRIPT, *allowed_skills],
            ):
                if hasattr(item, "exit_code"):
                    result = item
            return result

        result = await asyncio.to_thread(_sync_materialize)
        if result is None or result.exit_code != 0:
            raise RuntimeError(
                f"materialize-skills.sh failed for sandbox '{sandbox_id}' "
                f"(allowed_skills={allowed_skills}): "
                f"{getattr(result, 'stderr', '<no exec result>')}"
            )

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
        """Delete the OpenShell sandbox(es) for agent_name and clean up resources.

        For an agent_name tracked in this process (self._sandbox_names),
        destroys the exact sandbox spawn() created.

        For an untracked agent_name -- orphans discovered by
        reconcile_orphaned_sandboxes() via a gateway query on a restarted
        process, where local tracking is by definition empty (issue #224) --
        queries the gateway for sandboxes carrying a matching
        _AGENT_NAME_LABEL_KEY label and destroys every match (a genuine
        agent_name should map to at most one live sandbox, but destroying
        every match is the correct cleanup behavior if a prior failed
        attempt left more than one). If none carry that label (e.g. a
        sandbox _do_list_active() returned by raw name because it predates
        this labeling scheme, or has no label for another reason), falls
        back to treating agent_name as a literal sandbox name.

        Provider detachment and task/virtual-host tracking cleanup only
        apply to the tracked case -- an untracked agent_name never had that
        state recorded in this process.
        """
        tracked_name = self._sandbox_names.get(agent_name)
        if tracked_name:
            candidate_names = [tracked_name]
        else:
            refs = await asyncio.to_thread(
                self._client.list,
                workspace=self._workspace,
                label_selector=f"{self._AGENT_NAME_LABEL_KEY}={agent_name}",
            )
            candidate_names = [ref.name for ref in refs] or [agent_name]
            logger.info(
                "agent '%s' not tracked locally -- resolved to sandbox name(s) "
                "%s for destroy (expected for orphan recovery; if unexpected, "
                "check for a typo'd or unknown agent_name)",
                agent_name,
                candidate_names,
            )

        for sandbox_name in candidate_names:
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
                        provider_id,
                        sandbox_name,
                        exc_info=True,
                    )

            try:
                await asyncio.to_thread(
                    self._client.delete, sandbox_name, workspace=self._workspace
                )
                logger.info(
                    "Destroyed OpenShell sandbox '%s' (agent=%s)", sandbox_name, agent_name
                )
            except Exception:
                logger.warning(
                    "Failed to destroy sandbox '%s' (agent=%s) — "
                    "sandbox retained for manual cleanup",
                    sandbox_name,
                    agent_name,
                    exc_info=True,
                )
                continue
            self._sandbox_names.pop(agent_name, None)
            self._sandbox_ids.pop(agent_name, None)
            self._virtual_hosts.pop(sandbox_name, None)

    async def _do_list_active(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """List active sandbox agent names by querying the gateway directly.

        Queries the gateway's durable sandbox state (ListSandboxes) instead of
        self._sandbox_names, which is populated only by spawn() calls made in
        the *current* process and is empty after a restart -- the exact
        crash-recovery scenario reconcile_orphaned_sandboxes() targets
        (issue #224).

        Args:
            labels: Optional label filter, ANDed together as a gateway
                label selector (e.g. "k1=v1,k2=v2").

        Returns:
            List of agent names with active sandboxes. Names are recovered
            from each sandbox's _AGENT_NAME_LABEL_KEY label (spawn() always
            sets it) rather than the gateway-assigned sandbox name itself,
            which is opaque and unrelated to agent_name -- the gateway
            enforces a 19-character limit on caller-supplied names, too
            short to encode most agent_name values, so CreateSandboxRequest.
            name is left for the gateway to assign (issue #224). Sandboxes
            missing the label (e.g. created before this scheme existed, or
            by another tool) are returned by their raw sandbox name instead
            of being silently dropped -- destroy() falls back to treating
            such a name literally, so this remains cleanable.
        """
        label_selector = ",".join(f"{key}={value}" for key, value in (labels or {}).items())
        refs = await asyncio.to_thread(
            self._client.list,
            workspace=self._workspace,
            label_selector=label_selector or None,
            # SandboxClient.list() defaults to limit=100 with no built-in
            # pagination here; raised well above expected orphan counts as a
            # cheap mitigation. True pagination is a follow-up if a workspace
            # ever legitimately exceeds this.
            limit=1000,
        )
        return [ref.labels.get(self._AGENT_NAME_LABEL_KEY) or ref.name for ref in refs]
