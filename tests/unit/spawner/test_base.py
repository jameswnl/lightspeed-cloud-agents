"""Unit tests for AgentSpawner base class."""

import asyncio

import pytest

from agents.spawner.base import AgentSpawner


class MockSpawner(AgentSpawner):
    """Test spawner that doesn't actually create containers."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.spawned = []
        self.destroyed = []

    async def _do_spawn(self, agent_name, image, env):
        self.spawned.append(agent_name)
        return f"http://{agent_name}:8080"

    async def _do_destroy(self, agent_name):
        self.destroyed.append(agent_name)


class FailingSpawner(AgentSpawner):
    """Spawner that always fails."""

    async def _do_spawn(self, agent_name, image, env):
        raise RuntimeError("Spawn failed")

    async def _do_destroy(self, agent_name):
        pass


class TestAgentSpawner:
    """Tests for the base AgentSpawner."""

    @pytest.mark.asyncio
    async def test_spawn_returns_endpoint(self) -> None:
        """Test that spawn returns an endpoint URL."""
        spawner = MockSpawner()
        endpoint = await spawner.spawn("test-agent", "image:latest")
        assert endpoint == "http://test-agent:8080"
        assert "test-agent" in spawner.spawned

    @pytest.mark.asyncio
    async def test_spawn_increments_active_count(self) -> None:
        """Test that spawning increments the active count."""
        spawner = MockSpawner()
        assert spawner.active_count == 0
        await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 1
        await spawner.spawn("a2", "image:latest")
        assert spawner.active_count == 2

    @pytest.mark.asyncio
    async def test_destroy_decrements_active_count(self) -> None:
        """Test that destroying decrements the active count."""
        spawner = MockSpawner()
        await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 1
        await spawner.destroy("a1")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_concurrency_cap_enforced(self) -> None:
        """Test that the concurrency cap prevents over-spawning."""
        spawner = MockSpawner(max_pods=2)
        await spawner.spawn("a1", "image:latest")
        await spawner.spawn("a2", "image:latest")
        with pytest.raises(RuntimeError, match="Concurrency cap"):
            await spawner.spawn("a3", "image:latest")

    @pytest.mark.asyncio
    async def test_failed_spawn_doesnt_leak_count(self) -> None:
        """Test that a failed spawn doesn't increment the active count."""
        spawner = FailingSpawner(max_pods=2)
        with pytest.raises(RuntimeError, match="Spawn failed"):
            await spawner.spawn("a1", "image:latest")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_destroy_below_zero_safe(self) -> None:
        """Test that destroying when count is 0 doesn't go negative."""
        spawner = MockSpawner()
        await spawner.destroy("nonexistent")
        assert spawner.active_count == 0

    @pytest.mark.asyncio
    async def test_spawn_with_env(self) -> None:
        """Test spawning with environment variables."""
        spawner = MockSpawner()
        endpoint = await spawner.spawn(
            "test", "image:latest",
            env={"OLLAMA_URL": "http://ollama:11434/v1"},
        )
        assert endpoint == "http://test:8080"
