"""Unit tests for PodmanSpawner."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from cloud_agents.spawner.podman_spawner import PodmanSpawner


class TestPodmanSpawnerInit:
    """Tests for PodmanSpawner initialization."""

    def test_default_network(self) -> None:
        """Test default network name."""
        spawner = PodmanSpawner()
        assert spawner._network == "cloud-agents"

    def test_custom_network(self) -> None:
        """Test custom network name."""
        spawner = PodmanSpawner(network="my-network")
        assert spawner._network == "my-network"

    def test_volume_mounts(self) -> None:
        """Test volume mount configuration."""
        mounts = {
            "/host/agent.yaml": "/app/agent.yaml",
            "/host/tools.py": "/app/tools/tools.py",
        }
        spawner = PodmanSpawner(volume_mounts=mounts)
        assert spawner._volume_mounts == mounts

    def test_empty_volume_mounts_by_default(self) -> None:
        """Test that volume_mounts is empty by default."""
        spawner = PodmanSpawner()
        assert spawner._volume_mounts == {}


class TestPodmanSpawnerWriteFile:
    """Tests for PodmanSpawner._do_write_file() via podman exec."""

    @pytest.mark.asyncio
    async def test_write_file_calls_podman_exec(self) -> None:
        """write_file uses podman exec with stdin piping."""
        spawner = PodmanSpawner()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await spawner._do_write_file("my-agent", "/tmp/test.txt", "hello")

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "podman" in cmd
            assert "exec" in cmd
            assert "-i" in cmd
            assert "agent-my-agent" in cmd
            assert call_args[1]["input"] == b"hello"
            assert call_args[1]["check"] is True

    @pytest.mark.asyncio
    async def test_write_file_raises_on_failure(self) -> None:
        """write_file raises RuntimeError when podman exec fails."""
        import subprocess

        spawner = PodmanSpawner()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "podman", stderr="permission denied"
            )
            with pytest.raises(RuntimeError, match="Failed to write"):
                await spawner._do_write_file("my-agent", "/tmp/test.txt", "content")


class TestPodmanSpawnerMCPSecretMounts:
    """Tests for MCP secret mount handling in PodmanSpawner."""

    def test_do_spawn_accepts_mcp_secret_mounts_parameter(self) -> None:
        """PodmanSpawner._do_spawn accepts the mcp_secret_mounts parameter."""
        import inspect

        sig = inspect.signature(PodmanSpawner._do_spawn)
        assert (
            "mcp_secret_mounts" in sig.parameters
        ), "PodmanSpawner._do_spawn must accept mcp_secret_mounts parameter"

    @pytest.mark.asyncio
    async def test_mcp_secret_mounts_raises_on_podman(self) -> None:
        """Podman spawner raises ValueError for secret-backed MCP headers."""
        spawner = PodmanSpawner(network="test")
        with pytest.raises(ValueError, match="not supported on Podman"):
            await spawner._do_spawn(
                "test-agent",
                "image:latest",
                {},
                mcp_secret_mounts=[("secret-name", "key", "/var/secrets/mcp/sn/key")],
            )


class TestPodmanSpawnerLabels:
    """Tests for label injection in PodmanSpawner."""

    @pytest.mark.asyncio
    async def test_spawned_container_has_runner_label(self) -> None:
        """Spawned container includes spawned-by=workflow-runner label."""
        mock_container = MagicMock()
        mock_container.reload.return_value = None
        mock_container.ports = {"8080/tcp": [{"HostPort": "12345"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.side_effect = Exception("not found")
        mock_podman_client.containers.run.return_value = mock_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn(
                "test-agent",
                "image:latest",
                {},
                labels={"cloud-agents/workflow-id": "wf-1"},
            )

            run_call = mock_podman_client.containers.run.call_args
            labels = run_call[1].get("labels", {})
            assert labels.get("spawned-by") == "workflow-runner"
            assert labels.get("cloud-agents/workflow-id") == "wf-1"

    @pytest.mark.asyncio
    async def test_spawned_container_has_runner_label_without_extra_labels(
        self,
    ) -> None:
        """Spawned container includes spawned-by label even with no extra labels."""
        mock_container = MagicMock()
        mock_container.reload.return_value = None
        mock_container.ports = {"8080/tcp": [{"HostPort": "12345"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.side_effect = Exception("not found")
        mock_podman_client.containers.run.return_value = mock_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn("test-agent", "image:latest", {})

            run_call = mock_podman_client.containers.run.call_args
            labels = run_call[1].get("labels", {})
            assert labels.get("spawned-by") == "workflow-runner"


class TestPodmanSpawnerClient:
    """Tests for PodmanSpawner._client() and CONTAINER_HOST / DOCKER_HOST env vars."""

    def test_client_uses_container_host_env(self) -> None:
        """CONTAINER_HOST set, PodmanClient called with base_url."""
        mock_podman_cls = MagicMock()
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with (
            patch.dict("os.environ", {"CONTAINER_HOST": "unix:///custom/podman.sock"}, clear=False),
            patch.dict(sys.modules, {"podman": mock_podman_module}),
        ):
            # Remove DOCKER_HOST if present to isolate CONTAINER_HOST
            import os

            os.environ.pop("DOCKER_HOST", None)

            spawner = PodmanSpawner(network="test")
            spawner._client()

            mock_podman_cls.assert_called_once_with(base_url="unix:///custom/podman.sock")

    def test_client_uses_docker_host_fallback(self) -> None:
        """No CONTAINER_HOST but DOCKER_HOST set, PodmanClient uses DOCKER_HOST."""
        mock_podman_cls = MagicMock()
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with (
            patch.dict("os.environ", {"DOCKER_HOST": "unix:///docker/host.sock"}, clear=False),
            patch.dict(sys.modules, {"podman": mock_podman_module}),
        ):
            import os

            os.environ.pop("CONTAINER_HOST", None)

            spawner = PodmanSpawner(network="test")
            spawner._client()

            mock_podman_cls.assert_called_once_with(base_url="unix:///docker/host.sock")

    def test_client_default_no_base_url(self) -> None:
        """Neither env var set, PodmanClient() called with no args."""
        mock_podman_cls = MagicMock()
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with (
            patch.dict("os.environ", {}, clear=False),
            patch.dict(sys.modules, {"podman": mock_podman_module}),
        ):
            import os

            os.environ.pop("CONTAINER_HOST", None)
            os.environ.pop("DOCKER_HOST", None)

            spawner = PodmanSpawner(network="test")
            spawner._client()

            mock_podman_cls.assert_called_once_with()

    def test_container_host_takes_precedence(self) -> None:
        """Both CONTAINER_HOST and DOCKER_HOST set; CONTAINER_HOST wins."""
        mock_podman_cls = MagicMock()
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with (
            patch.dict(
                "os.environ",
                {
                    "CONTAINER_HOST": "unix:///container/host.sock",
                    "DOCKER_HOST": "unix:///docker/host.sock",
                },
                clear=False,
            ),
            patch.dict(sys.modules, {"podman": mock_podman_module}),
        ):
            spawner = PodmanSpawner(network="test")
            spawner._client()

            mock_podman_cls.assert_called_once_with(base_url="unix:///container/host.sock")


class TestPodmanSpawnerTLS:
    """Tests for TLS cert injection in PodmanSpawner."""

    def _make_mock_podman(self) -> tuple:
        """Create a mock Podman client and container.

        Returns:
            Tuple of (mock_podman_module, mock_podman_client, mock_container).
        """
        mock_container = MagicMock()
        mock_container.reload.return_value = None
        mock_container.ports = {"8443/tcp": [{"HostPort": "54321"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.side_effect = Exception("not found")
        mock_podman_client.containers.run.return_value = mock_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        return mock_podman_module, mock_podman_client, mock_container

    def _make_tls_certs(self) -> "EphemeralCerts":
        """Create a mock EphemeralCerts for testing."""
        from cloud_agents.workflow.tls import EphemeralCerts

        return EphemeralCerts(
            ca_cert_pem=b"-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
            server_cert_pem=b"-----BEGIN CERTIFICATE-----\nSERVER\n-----END CERTIFICATE-----\n",
            server_key_pem=b"mock-server-key-pem-data",
            valid_seconds=600,
        )

    @pytest.mark.asyncio
    async def test_tls_certs_creates_temp_dir_with_files(self) -> None:
        """TLS certs provided -> temp dir created with cert and key files."""
        mock_podman_module, mock_podman_client, _ = self._make_mock_podman()
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch("cloud_agents.spawner.podman_spawner.tempfile.mkdtemp") as mock_mkdtemp,
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
        ):
            mock_mkdtemp.return_value = "/tmp/sandbox-tls-xyz"
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn(
                "test-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            mock_mkdtemp.assert_called_once()
            assert "test-agent" in spawner._tls_temp_dirs

    @pytest.mark.asyncio
    async def test_tls_certs_changes_port_to_8443(self) -> None:
        """TLS certs provided -> container port changed to 8443."""
        mock_podman_module, mock_podman_client, _ = self._make_mock_podman()
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch(
                "cloud_agents.spawner.podman_spawner.tempfile.mkdtemp", return_value="/tmp/tls-xyz"
            ),
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
        ):
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn(
                "tls-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            run_call = mock_podman_client.containers.run.call_args
            ports = run_call[1].get("ports", {})
            assert "8443/tcp" in ports

    @pytest.mark.asyncio
    async def test_tls_certs_sets_env_vars(self) -> None:
        """TLS certs provided -> SANDBOX_TLS_CERT_PATH and _KEY_PATH env vars set."""
        mock_podman_module, mock_podman_client, _ = self._make_mock_podman()
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch(
                "cloud_agents.spawner.podman_spawner.tempfile.mkdtemp", return_value="/tmp/tls-xyz"
            ),
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
        ):
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn(
                "tls-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            run_call = mock_podman_client.containers.run.call_args
            env = run_call[1].get("environment", {})
            assert env.get("SANDBOX_TLS_CERT_PATH") == "/var/run/secrets/sandbox-tls/tls.crt"
            assert env.get("SANDBOX_TLS_KEY_PATH") == "/var/run/secrets/sandbox-tls/tls.key"

    @pytest.mark.asyncio
    async def test_tls_certs_mounts_volume(self) -> None:
        """TLS certs provided -> temp dir bind-mounted at /var/run/secrets/sandbox-tls/."""
        mock_podman_module, mock_podman_client, _ = self._make_mock_podman()
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch(
                "cloud_agents.spawner.podman_spawner.tempfile.mkdtemp", return_value="/tmp/tls-xyz"
            ),
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
        ):
            spawner = PodmanSpawner(network="test")
            await spawner._do_spawn(
                "tls-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            run_call = mock_podman_client.containers.run.call_args
            volumes = run_call[1].get("volumes", {})
            assert "/tmp/tls-xyz" in volumes
            mount = volumes["/tmp/tls-xyz"]
            assert mount["bind"] == "/var/run/secrets/sandbox-tls/"
            assert mount["mode"] == "ro"

    @pytest.mark.asyncio
    async def test_tls_certs_endpoint_uses_https(self) -> None:
        """TLS certs provided -> endpoint URL uses https scheme."""
        mock_podman_module, mock_podman_client, mock_container = self._make_mock_podman()
        mock_container.ports = {"8443/tcp": [{"HostPort": "54321"}]}
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch(
                "cloud_agents.spawner.podman_spawner.tempfile.mkdtemp", return_value="/tmp/tls-xyz"
            ),
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
        ):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn(
                "tls-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            assert endpoint.startswith("https://")

    @pytest.mark.asyncio
    async def test_tls_certs_remote_endpoint_uses_port_8443(self) -> None:
        """TLS certs with remote podman URL -> endpoint uses https and port 8443."""
        mock_podman_module, mock_podman_client, mock_container = self._make_mock_podman()
        tls_certs = self._make_tls_certs()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch(
                "cloud_agents.spawner.podman_spawner.tempfile.mkdtemp", return_value="/tmp/tls-xyz"
            ),
            patch("builtins.open", MagicMock()),
            patch("os.chmod"),
            patch.dict("os.environ", {"CONTAINER_HOST": "unix:///custom.sock"}),
        ):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn(
                "tls-agent",
                "image:latest",
                {},
                tls_certs=tls_certs,
            )

            assert endpoint.startswith("https://")
            assert ":8443" in endpoint

    @pytest.mark.asyncio
    async def test_no_tls_certs_no_changes(self) -> None:
        """tls_certs=None -> no TLS changes (backward compat)."""
        mock_container = MagicMock()
        mock_container.reload.return_value = None
        mock_container.ports = {"8080/tcp": [{"HostPort": "12345"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.side_effect = Exception("not found")
        mock_podman_client.containers.run.return_value = mock_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn(
                "no-tls-agent",
                "image:latest",
                {},
            )

            assert endpoint.startswith("http://")
            assert "8080" in endpoint or "12345" in endpoint
            run_call = mock_podman_client.containers.run.call_args
            ports = run_call[1].get("ports", {})
            assert "8080/tcp" in ports
            env = run_call[1].get("environment", {})
            assert "SANDBOX_TLS_CERT_PATH" not in env
            assert "SANDBOX_TLS_KEY_PATH" not in env

    @pytest.mark.asyncio
    async def test_destroy_cleans_up_tls_temp_dir(self) -> None:
        """Destroy cleans up TLS temp directory."""
        mock_podman_module, mock_podman_client, _ = self._make_mock_podman()

        with (
            patch.dict(sys.modules, {"podman": mock_podman_module}),
            patch("cloud_agents.spawner.podman_spawner.shutil.rmtree") as mock_rmtree,
        ):
            spawner = PodmanSpawner(network="test")
            spawner._tls_temp_dirs["test-agent"] = "/tmp/tls-test-agent"

            await spawner._do_destroy("test-agent")

            mock_rmtree.assert_called_once_with("/tmp/tls-test-agent", ignore_errors=True)
            assert "test-agent" not in spawner._tls_temp_dirs


class TestPodmanSpawnerIdempotentTLS:
    """Tests for idempotent container reuse with TLS ports."""

    @pytest.mark.asyncio
    async def test_idempotent_reuse_with_tls_port_8443(self) -> None:
        """Idempotent path detects 8443/tcp and returns https:// URL."""
        existing_container = MagicMock()
        existing_container.status = "running"
        existing_container.reload.return_value = None
        existing_container.ports = {"8443/tcp": [{"HostPort": "54321"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.return_value = existing_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn("tls-agent", "image:latest", {})

            assert endpoint == "https://localhost:54321"

    @pytest.mark.asyncio
    async def test_idempotent_reuse_with_plain_port_8080(self) -> None:
        """Idempotent path detects 8080/tcp and returns http:// URL."""
        existing_container = MagicMock()
        existing_container.status = "running"
        existing_container.reload.return_value = None
        existing_container.ports = {"8080/tcp": [{"HostPort": "12345"}]}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.return_value = existing_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn("plain-agent", "image:latest", {})

            assert endpoint == "http://localhost:12345"

    @pytest.mark.asyncio
    async def test_idempotent_reuse_tls_no_host_port(self) -> None:
        """Idempotent path with 8443/tcp but no host port returns container name with https."""
        existing_container = MagicMock()
        existing_container.status = "running"
        existing_container.reload.return_value = None
        existing_container.ports = {"8443/tcp": []}

        mock_podman_client = MagicMock()
        mock_podman_client.__enter__ = MagicMock(return_value=mock_podman_client)
        mock_podman_client.__exit__ = MagicMock(return_value=False)
        mock_podman_client.containers.get.return_value = existing_container

        mock_podman_cls = MagicMock(return_value=mock_podman_client)
        mock_podman_module = types.ModuleType("podman")
        mock_podman_module.PodmanClient = mock_podman_cls

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner(network="test")
            endpoint = await spawner._do_spawn("tls-agent", "image:latest", {})

            assert endpoint == "https://agent-tls-agent:8443"


class TestPodmanSpawnerDestroy:
    """Tests for PodmanSpawner destroy behavior."""

    @pytest.mark.asyncio
    async def test_destroy_handles_missing_container(self) -> None:
        """Test that destroying a nonexistent container logs warning, not crash."""
        spawner = PodmanSpawner()
        # Should not raise even if container doesn't exist
        await spawner._do_destroy("nonexistent-agent")

    @pytest.mark.asyncio
    async def test_destroy_without_podman_installed(self) -> None:
        """Test graceful handling when podman-py is not installed."""
        spawner = PodmanSpawner()
        with patch.dict("sys.modules", {"podman": None}):
            # The import inside _do_destroy will fail gracefully
            await spawner._do_destroy("test-agent")
