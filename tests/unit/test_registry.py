"""Unit tests for AgentRegistry."""

import pytest

from agents.registry import AgentRegistry


class TestAgentRegistry:
    """Tests for agent endpoint registry."""

    def test_get_endpoint_known_agent(self) -> None:
        """Test lookup returns correct endpoint URL."""
        registry = AgentRegistry({"diagnostic-agent": "http://diag:8080"})
        assert registry.get_endpoint("diagnostic-agent") == "http://diag:8080"

    def test_get_endpoint_unknown_agent(self) -> None:
        """Test lookup raises ValueError for unknown agent."""
        registry = AgentRegistry({})
        with pytest.raises(ValueError, match="not configured"):
            registry.get_endpoint("nonexistent")

    def test_list_agents_empty(self) -> None:
        """Test list_agents on empty registry."""
        registry = AgentRegistry({})
        assert registry.list_agents() == []

    def test_list_agents_multiple(self) -> None:
        """Test list_agents with multiple agents."""
        registry = AgentRegistry(
            {
                "diag": "http://diag:8080",
                "monitor": "http://monitor:8080",
            }
        )
        agents = registry.list_agents()
        assert set(agents) == {"diag", "monitor"}

    def test_has_agent_true(self) -> None:
        """Test has_agent returns True for known agent."""
        registry = AgentRegistry({"diag": "http://diag:8080"})
        assert registry.has_agent("diag") is True

    def test_has_agent_false(self) -> None:
        """Test has_agent returns False for unknown agent."""
        registry = AgentRegistry({})
        assert registry.has_agent("diag") is False
