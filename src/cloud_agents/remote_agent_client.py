"""HTTP client for calling remote agent pods.

Replaces in-process agent delegation with cross-pod HTTP communication.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from cloud_agents.exceptions import AgentError, AgentTimeoutError, AgentUnavailableError
from cloud_agents.models import AgentRunResponse, RunState, RunStatus
from cloud_agents.runtime.tracing import inject_traceparent


class RemoteAgentClient:
    """Async HTTP client for calling an agent pod's /v1/run endpoint.

    Attributes:
        endpoint: Base URL of the agent pod (e.g. ``http://diagnostic-agent:8080``).
        timeout: Request timeout in seconds.
    """

    def __init__(
        self, endpoint: str, timeout: float = 600.0, auth_token: Optional[str] = None
    ) -> None:
        """Initialize the client.

        Args:
            endpoint: Base URL of the agent pod.
            timeout: Request timeout in seconds.
            auth_token: Optional bearer token for authenticated requests.
        """
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._auth_token = auth_token

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

        headers: dict[str, str] = {}
        inject_traceparent(headers)
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                response = await client.post(
                    f"{self.endpoint}/v1/run",
                    json=body,
                    headers=headers,
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
            raise AgentError(f"Agent '{result.agent_name}' run failed: {result.error}")

        return result

    async def run_async(
        self,
        prompt: str,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Submit a prompt asynchronously and return the run_id.

        Args:
            prompt: The task or question for the agent.
            context: Optional metadata.

        Returns:
            The run_id for polling.

        Raises:
            AgentUnavailableError: If the agent pod cannot be reached.
            AgentError: If the submission fails.
        """
        body: dict[str, Any] = {"prompt": prompt}
        if context is not None:
            body["context"] = context

        async_headers: dict[str, str] = {}
        inject_traceparent(async_headers)
        if self._auth_token:
            async_headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                response = await client.post(
                    f"{self.endpoint}/v1/run",
                    json=body,
                    headers={**async_headers, "Prefer": "respond-async"},
                )
        except httpx.ConnectError as exc:
            raise AgentUnavailableError(
                f"Agent at {self.endpoint} unavailable: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentError(
                f"HTTP error submitting to agent at {self.endpoint}: {exc}"
            ) from exc

        if response.status_code != 202:
            raise AgentError(
                f"Expected 202 from async submit, got {response.status_code}: "
                f"{response.text}"
            )

        data = response.json()
        return data["run_id"]

    async def poll_run(self, run_id: str) -> RunState:
        """Poll the status of an async run.

        Args:
            run_id: The run identifier from run_async().

        Returns:
            Current RunState.

        Raises:
            AgentError: If the run is not found or the poll fails.
        """
        poll_headers: dict[str, str] = {}
        if self._auth_token:
            poll_headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(
                    f"{self.endpoint}/v1/runs/{run_id}",
                    headers=poll_headers or None,
                )
        except httpx.HTTPError as exc:
            raise AgentError(
                f"Error polling run {run_id} at {self.endpoint}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise AgentError(f"Run {run_id} not found at {self.endpoint}")

        return RunState.model_validate(response.json())

    async def run_with_polling(
        self,
        prompt: str,
        context: Optional[dict[str, Any]] = None,
        poll_interval: float = 2.0,
    ) -> AgentRunResponse:
        """Submit async and poll until completion.

        Args:
            prompt: The task or question for the agent.
            context: Optional metadata.
            poll_interval: Seconds between polls.

        Returns:
            The completed AgentRunResponse.

        Raises:
            AgentTimeoutError: If the run doesn't complete within the timeout.
            AgentError: If the run fails.
        """
        import asyncio
        import time

        run_id = await self.run_async(prompt, context=context)
        start = time.monotonic()

        while time.monotonic() - start < self.timeout:
            state = await self.poll_run(run_id)
            if state.status == RunStatus.COMPLETED:
                if state.result is None:
                    raise AgentError(f"Run {run_id} completed but no result")
                if not state.result.success:
                    raise AgentError(
                        f"Agent '{state.result.agent_name}' run failed: "
                        f"{state.result.error}"
                    )
                return state.result
            if state.status == RunStatus.FAILED:
                error = state.result.error if state.result else "Unknown error"
                raise AgentError(f"Run {run_id} failed: {error}")
            await asyncio.sleep(poll_interval)

        raise AgentTimeoutError(f"Run {run_id} did not complete within {self.timeout}s")

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
