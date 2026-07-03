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
                mcp_secret_mounts=[
                    ("secret-name", "key", "/var/secrets/mcp/sn/key")
                ],
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

            mock_podman_cls.assert_called_once_with(
                base_url="unix:///custom/podman.sock"
            )

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

            mock_podman_cls.assert_called_once_with(
                base_url="unix:///docker/host.sock"
            )

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

            mock_podman_cls.assert_called_once_with(
                base_url="unix:///container/host.sock"
            )


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
