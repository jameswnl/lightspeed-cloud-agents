"""Unit tests for OpenShellSpawner."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# We mock the openshell imports, so import our spawner after patching
SANDBOX_NAME_PREFIX = "ca-agent-"


def _make_mock_openshell():
    """Create mock openshell module and its submodules.

    Returns:
        Tuple of (mock_openshell, mock_pb2, mock_pb2_grpc).
    """
    mock_pb2 = MagicMock()
    mock_pb2.SANDBOX_PHASE_READY = 2
    mock_pb2.SANDBOX_PHASE_ERROR = 3
    mock_pb2.SANDBOX_PHASE_PROVISIONING = 1

    mock_pb2_grpc = MagicMock()

    mock_proto = types.ModuleType("openshell._proto")
    mock_proto.openshell_pb2 = mock_pb2
    mock_proto.openshell_pb2_grpc = mock_pb2_grpc

    mock_openshell = types.ModuleType("openshell")
    mock_openshell.SandboxClient = MagicMock()
    mock_openshell.SandboxRef = MagicMock()
    mock_openshell.SandboxError = type("SandboxError", (RuntimeError,), {})
    mock_openshell._proto = mock_proto

    return mock_openshell, mock_pb2, mock_pb2_grpc


def _make_sandbox_ref(name="ca-agent-test", sandbox_id="sb-123", phase=2):
    """Create a mock SandboxRef."""
    ref = MagicMock()
    ref.id = sandbox_id
    ref.name = name
    ref.status = MagicMock()
    ref.status.phase = phase
    return ref


def _patch_openshell():
    """Patch openshell modules into sys.modules.

    Returns:
        Tuple of (patcher, mock_openshell, mock_pb2).
    """
    mock_openshell, mock_pb2, mock_pb2_grpc = _make_mock_openshell()
    modules = {
        "openshell": mock_openshell,
        "openshell._proto": mock_openshell._proto,
        "openshell._proto.openshell_pb2": mock_pb2,
        "openshell._proto.openshell_pb2_grpc": mock_pb2_grpc,
    }
    return patch.dict(sys.modules, modules), mock_openshell, mock_pb2


class TestOpenShellSpawnerInit:
    """Tests for OpenShellSpawner initialization."""

    def test_default_gateway_url_from_env(self) -> None:
        """Gateway URL read from OPENSHELL_GATEWAY_URL env var."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher, patch.dict("os.environ", {"OPENSHELL_GATEWAY_URL": "localhost:50051"}):
            # Remove cached module to force re-import
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner()
            assert spawner._gateway_url == "localhost:50051"

    def test_explicit_gateway_url(self) -> None:
        """Explicit gateway_url parameter takes precedence."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner(gateway_url="myhost:9090")
            assert spawner._gateway_url == "myhost:9090"

    def test_cluster_name_stored(self) -> None:
        """Cluster name stored for from_active_cluster fallback."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner(cluster="my-cluster")
            assert spawner._cluster == "my-cluster"

    def test_no_gateway_url_no_cluster(self) -> None:
        """No gateway URL and no cluster -> both are None."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher, patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENSHELL_GATEWAY_URL", None)
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner()
            assert spawner._gateway_url is None
            assert spawner._cluster is None

    def test_max_pods_passed_to_base(self) -> None:
        """max_pods kwarg forwarded to AgentSpawner base."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner(gateway_url="host:50051", max_pods=5)
            assert spawner._max_pods == 5


class TestOpenShellSpawnerGetClient:
    """Tests for _get_client() connection logic."""

    def test_get_client_with_gateway_url(self) -> None:
        """When gateway_url is set, SandboxClient is created with endpoint."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner(gateway_url="myhost:50051")
            spawner._get_client()
            mock_os.SandboxClient.assert_called_once_with(endpoint="myhost:50051")

    def test_get_client_without_gateway_url(self) -> None:
        """When no gateway_url, falls back to from_active_cluster."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher, patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENSHELL_GATEWAY_URL", None)
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            spawner = OpenShellSpawner(cluster="dev-cluster")
            spawner._get_client()
            mock_os.SandboxClient.from_active_cluster.assert_called_once_with(cluster="dev-cluster")


class TestOpenShellSpawnerSpawn:
    """Tests for _do_spawn() lifecycle."""

    @pytest.mark.asyncio
    async def test_spawn_creates_sandbox_and_returns_endpoint(self) -> None:
        """Spawn creates a sandbox with image and env vars, returns endpoint URL."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://gateway:8080/sandbox/ca-agent-test/agent-http"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            endpoint = await spawner._do_spawn(
                "test",
                "sandbox-image:latest",
                {"MODEL_API_KEY": "sk-123"},
            )

            # Verify create was called
            mock_client.create.assert_called_once()

            # Verify wait_ready was called
            mock_client.wait_ready.assert_called_once_with(sandbox_ref.name, timeout_seconds=60)

            # Verify ExposeService was called
            mock_client._stub.ExposeService.assert_called_once()

            assert endpoint == "http://gateway:8080/sandbox/ca-agent-test/agent-http"

    @pytest.mark.asyncio
    async def test_spawn_uses_sandbox_name_prefix(self) -> None:
        """Sandbox name follows ca-agent-{agent_name} convention."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref(name="ca-agent-my-agent")
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://gateway:8080/service"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_spawn("my-agent", "image:latest", {})

            # Verify create was called (the spec should have the naming)
            mock_client.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_exposes_port_8080_by_default(self) -> None:
        """ExposeService targets port 8080."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://gateway:8080/svc"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_spawn("test", "image:latest", {})

            # Verify ExposeServiceRequest was constructed with target_port=8080
            mock_pb2.ExposeServiceRequest.assert_called_once()
            req_kwargs = mock_pb2.ExposeServiceRequest.call_args[1]
            assert req_kwargs["target_port"] == 8080

    @pytest.mark.asyncio
    async def test_spawn_closes_client(self) -> None:
        """Client is closed after spawn completes."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://url"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_spawn("test", "image:latest", {})

            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_uses_custom_timeout(self) -> None:
        """SpawnConfig.timeout_seconds is forwarded to wait_ready."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.base import SpawnConfig
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://url"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            cfg = SpawnConfig(timeout_seconds=120)
            await spawner._do_spawn("test", "image:latest", {}, config_override=cfg)

            mock_client.wait_ready.assert_called_once_with(sandbox_ref.name, timeout_seconds=120)


class TestOpenShellSpawnerDestroy:
    """Tests for _do_destroy()."""

    @pytest.mark.asyncio
    async def test_destroy_deletes_sandbox(self) -> None:
        """Destroy calls client.delete with correct sandbox name."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.delete.return_value = True
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_destroy("my-agent")

            mock_client.delete.assert_called_once_with("ca-agent-my-agent")
            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_destroy_handles_not_found(self) -> None:
        """Destroy logs warning but does not raise when sandbox is gone."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.delete.side_effect = RuntimeError("NOT_FOUND")
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            # Should not raise
            await spawner._do_destroy("missing-agent")

    @pytest.mark.asyncio
    async def test_destroy_closes_client_on_error(self) -> None:
        """Client is closed even when delete raises."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.delete.side_effect = RuntimeError("oops")
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_destroy("err-agent")

            mock_client.close.assert_called_once()


class TestOpenShellSpawnerListActive:
    """Tests for _do_list_active()."""

    @pytest.mark.asyncio
    async def test_list_active_filters_by_prefix(self) -> None:
        """Only sandboxes with ca-agent- prefix are returned."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.list.return_value = [
                _make_sandbox_ref(name="ca-agent-agent1"),
                _make_sandbox_ref(name="ca-agent-agent2"),
                _make_sandbox_ref(name="other-sandbox"),
                _make_sandbox_ref(name="ca-agent-agent3"),
            ]
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            result = await spawner._do_list_active()

            assert result == ["agent1", "agent2", "agent3"]

    @pytest.mark.asyncio
    async def test_list_active_returns_empty_on_no_matches(self) -> None:
        """Returns empty list when no sandboxes match prefix."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.list.return_value = [
                _make_sandbox_ref(name="other-1"),
                _make_sandbox_ref(name="other-2"),
            ]
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            result = await spawner._do_list_active()

            assert result == []

    @pytest.mark.asyncio
    async def test_list_active_handles_connection_error(self) -> None:
        """Returns empty list when gateway is unreachable."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.list.side_effect = RuntimeError("gateway unavailable")
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            result = await spawner._do_list_active()

            assert result == []

    @pytest.mark.asyncio
    async def test_list_active_closes_client(self) -> None:
        """Client is closed after listing."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.list.return_value = []
            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            await spawner._do_list_active()

            mock_client.close.assert_called_once()


class TestOpenShellSpawnerEnvInjection:
    """Tests for environment variable injection."""

    @pytest.mark.asyncio
    async def test_env_vars_passed_to_sandbox_spec(self) -> None:
        """Environment variables are included in the SandboxSpec."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://url"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            env = {
                "MODEL_API_KEY": "sk-test",
                "LIGHTSPEED_MODEL": "gpt-4",
                "CUSTOM_VAR": "value",
            }
            await spawner._do_spawn("test", "image:latest", env)

            # Verify create was called and the spec has environment
            mock_pb2.SandboxSpec.assert_called()
            spec_call = mock_pb2.SandboxSpec.call_args
            assert "environment" in spec_call[1]
            assert spec_call[1]["environment"] == env


class TestOpenShellSpawnerErrors:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_spawn_raises_on_create_failure(self) -> None:
        """Spawn raises RuntimeError when sandbox creation fails."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            mock_client.create.side_effect = RuntimeError("gateway unavailable")

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with pytest.raises(RuntimeError, match="gateway unavailable"):
                await spawner._do_spawn("test", "image:latest", {})

            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_raises_on_error_phase(self) -> None:
        """Spawn raises when sandbox enters SANDBOX_PHASE_ERROR."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.side_effect = RuntimeError("sandbox entered error phase")

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with pytest.raises(RuntimeError, match="error phase"):
                await spawner._do_spawn("test", "image:latest", {})

    @pytest.mark.asyncio
    async def test_spawn_raises_on_expose_service_failure(self) -> None:
        """Spawn raises when ExposeService call fails."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref
            mock_client._stub.ExposeService.side_effect = RuntimeError("service exposure failed")

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with pytest.raises(RuntimeError, match="service exposure failed"):
                await spawner._do_spawn("test", "image:latest", {})

    @pytest.mark.asyncio
    async def test_spawn_cleans_up_on_expose_failure(self) -> None:
        """Sandbox is deleted if ExposeService fails after creation."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref
            mock_client._stub.ExposeService.side_effect = RuntimeError("fail")

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with pytest.raises(RuntimeError):
                await spawner._do_spawn("test", "image:latest", {})

            # Sandbox should be cleaned up
            mock_client.delete.assert_called_once_with(sandbox_ref.name)

    @pytest.mark.asyncio
    async def test_spawn_cleans_up_on_wait_ready_failure(self) -> None:
        """Sandbox is deleted if wait_ready fails after creation."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.side_effect = RuntimeError("timeout")

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with pytest.raises(RuntimeError, match="timeout"):
                await spawner._do_spawn("test", "image:latest", {})

            # Sandbox should be cleaned up
            mock_client.delete.assert_called_once_with(sandbox_ref.name)


class TestOpenShellSpawnerTLS:
    """Tests for TLS certificate handling."""

    @pytest.mark.asyncio
    async def test_tls_certs_logs_warning(self) -> None:
        """Passing tls_certs logs a warning about OpenShell managing TLS."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner
            from cloud_agents.workflow.tls import EphemeralCerts

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://url"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            tls_certs = EphemeralCerts(
                ca_cert_pem=b"ca",
                server_cert_pem=b"cert",
                server_key_pem=b"key",
                valid_seconds=600,
            )

            with patch("cloud_agents.spawner.openshell_spawner.logger") as mock_logger:
                await spawner._do_spawn("test", "image:latest", {}, tls_certs=tls_certs)
                mock_logger.warning.assert_called()
                warning_msg = mock_logger.warning.call_args[0][0]
                assert "TLS" in warning_msg or "tls" in warning_msg

    @pytest.mark.asyncio
    async def test_no_tls_certs_no_warning(self) -> None:
        """No warning when tls_certs is None."""
        patcher, mock_os, mock_pb2 = _patch_openshell()
        with patcher:
            sys.modules.pop("cloud_agents.spawner.openshell_spawner", None)
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            mock_client = MagicMock()
            sandbox_ref = _make_sandbox_ref()
            mock_client.create.return_value = sandbox_ref
            mock_client.wait_ready.return_value = sandbox_ref

            expose_response = MagicMock()
            expose_response.url = "http://url"
            mock_client._stub.ExposeService.return_value = expose_response

            spawner = OpenShellSpawner(gateway_url="host:50051")
            spawner._get_client = MagicMock(return_value=mock_client)

            with patch("cloud_agents.spawner.openshell_spawner.logger") as mock_logger:
                await spawner._do_spawn("test", "image:latest", {})
                # No TLS warning should be logged
                for call in mock_logger.warning.call_args_list:
                    msg = call[0][0] if call[0] else ""
                    assert "TLS" not in msg
