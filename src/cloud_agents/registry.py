"""Agent endpoint registry.

Maps agent names to their HTTP endpoints. Populated from configuration
at core pod startup.
"""

from __future__ import annotations


class AgentRegistry:
    """In-memory registry of agent pod endpoints.

    Attributes:
        _agents: Mapping of agent name to endpoint URL.
    """

    def __init__(self, agents: dict[str, str]) -> None:
        """Initialize the registry.

        Args:
            agents: Mapping of agent name to endpoint URL.
        """
        self._agents = dict(agents)

    def get_endpoint(self, agent_name: str) -> str:
        """Look up the endpoint URL for an agent.

        Args:
            agent_name: The agent to look up.

        Returns:
            The endpoint URL.

        Raises:
            ValueError: If the agent is not configured.
        """
        if agent_name not in self._agents:
            raise ValueError(f"Agent '{agent_name}' not configured")
        return self._agents[agent_name]

    def list_agents(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())

    def has_agent(self, agent_name: str) -> bool:
        """Check if an agent is registered."""
        return agent_name in self._agents
