"""HTTP client for calling remote agent pods.

Replaces in-process agent delegation with cross-pod HTTP communication.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from agents.exceptions import AgentError, AgentTimeoutError, AgentUnavailableError
from agents.models import AgentRunResponse


class RemoteAgentClient:
    """Async HTTP client for calling an agent pod's /v1/run endpoint.

    Attributes:
        endpoint: Base URL of the agent pod (e.g. ``http://diagnostic-agent:8080``).
        timeout: Request timeout in seconds.
    """

    def __init__(self, endpoint: str, timeout: float = 600.0) -> None:
        """Initialize the client.

        Args:
            endpoint: Base URL of the agent pod.
            timeout: Request timeout in seconds.
        """
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    async def run(
        self,
        prompt: str,
        context: Optional[dict[str, Any]] = None,
    ) -> AgentRunResponse:
        """Send a prompt to the agent pod and return the structured response.

        Args:
            prompt: The task or question for the agent.
            context: Optional metadata (correlation_id, trace_id, etc.).

        Returns:
            AgentRunResponse from the agent pod.

        Raises:
            AgentTimeoutError: If the agent does not respond within the timeout.
            AgentUnavailableError: If the agent pod cannot be reached.
            AgentError: If the agent returns a non-200 response or invalid data.
        """
        body: dict[str, Any] = {"prompt": prompt}
        if context is not None:
            body["context"] = context

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout)
            ) as client:
                response = await client.post(
                    f"{self.endpoint}/v1/run",
                    json=body,
                )
        except httpx.ReadTimeout as exc:
            raise AgentTimeoutError(
                f"Agent at {self.endpoint} timed out after {self.timeout}s: {exc}"
            ) from exc
        except httpx.ConnectError as exc:
            raise AgentUnavailableError(
                f"Agent at {self.endpoint} unavailable: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentError(
                f"HTTP error communicating with agent at {self.endpoint}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise AgentError(
                f"Agent at {self.endpoint} returned {response.status_code}: "
                f"{response.text}"
            )

        try:
            data = response.json()
            result = AgentRunResponse.model_validate(data)
        except Exception as exc:
            raise AgentError(
                f"Invalid response from agent at {self.endpoint}: {exc}"
            ) from exc

        if not result.success:
            raise AgentError(
                f"Agent '{result.agent_name}' run failed: {result.error}"
            )

        return result

    async def healthz(self) -> bool:
        """Check if the agent pod is ready.

        Returns:
            True if the agent responds with 200, False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(f"{self.endpoint}/healthz")
                return response.status_code == 200
        except httpx.HTTPError:
            return False
