"""Unit tests for PodmanSpawner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.spawner.podman_spawner import PodmanSpawner


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
