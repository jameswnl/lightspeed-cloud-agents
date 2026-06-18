"""Exception types for cloud agent communication."""


class AgentError(Exception):
    """Base exception for agent communication errors."""


class AgentTimeoutError(AgentError):
    """Raised when an agent pod does not respond within the timeout."""


class AgentUnavailableError(AgentError):
    """Raised when an agent pod cannot be reached (connection refused, DNS failure)."""
