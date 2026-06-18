"""Unit tests for RemoteAgentClient."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch

from agents.exceptions import AgentError, AgentTimeoutError, AgentUnavailableError
from agents.models import AgentRunResponse, RunState, RunStatus
from agents.remote_agent_client import RemoteAgentClient


MOCK_SUCCESS_RESPONSE = {
    "output": {"summary": "Fixed", "issues_found": ["x"], "actions_taken": [], "cluster_healthy": True},
    "output_type": "DiagnosticReport",
    "schema_version": "v1",
    "usage": {"input_tokens": 100, "output_tokens": 200},
    "agent_name": "diagnostic-agent",
    "success": True,
    "error": None,
}


class TestRemoteAgentClientRun:
    """Tests for RemoteAgentClient.run()."""

    @pytest.mark.asyncio
    async def test_successful_run(self) -> None:
        """Test successful agent call returns AgentRunResponse."""
        mock_response = httpx.Response(200, json=MOCK_SUCCESS_RESPONSE)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            result = await client.run("Check hosts")

        assert isinstance(result, AgentRunResponse)
        assert result.success is True
        assert result.agent_name == "diagnostic-agent"
        assert result.output_type == "DiagnosticReport"

    @pytest.mark.asyncio
    async def test_run_with_context(self) -> None:
        """Test that context is passed through in the request."""
        mock_response = httpx.Response(200, json=MOCK_SUCCESS_RESPONSE)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ) as mock_post:
            client = RemoteAgentClient("http://agent:8080")
            await client.run("Check hosts", context={"correlation_id": "abc"})

        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert body["context"]["correlation_id"] == "abc"

    @pytest.mark.asyncio
    async def test_timeout_raises_agent_timeout_error(self) -> None:
        """Test that a timeout raises AgentTimeoutError."""
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ReadTimeout("timed out"),
        ):
            client = RemoteAgentClient("http://agent:8080", timeout=5.0)
            with pytest.raises(AgentTimeoutError, match="timed out"):
                await client.run("Check hosts")

    @pytest.mark.asyncio
    async def test_connection_error_raises_agent_unavailable(self) -> None:
        """Test that a connection error raises AgentUnavailableError."""
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            client = RemoteAgentClient("http://agent:8080")
            with pytest.raises(AgentUnavailableError, match="Connection refused"):
                await client.run("Check hosts")

    @pytest.mark.asyncio
    async def test_500_raises_agent_error(self) -> None:
        """Test that a 500 response raises AgentError."""
        mock_response = httpx.Response(
            500,
            json={"detail": "Agent run failed: RuntimeError: LLM down"},
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            with pytest.raises(AgentError, match="500"):
                await client.run("Check hosts")

    @pytest.mark.asyncio
    async def test_malformed_json_raises_agent_error(self) -> None:
        """Test that a malformed JSON response raises AgentError."""
        mock_response = httpx.Response(200, text="not json")
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            with pytest.raises(AgentError, match="response"):
                await client.run("Check hosts")

    @pytest.mark.asyncio
    async def test_success_false_raises_agent_error(self) -> None:
        """Test that a 200 response with success=False raises AgentError."""
        error_response = {
            "output": {},
            "output_type": "error",
            "schema_version": "v1",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "agent_name": "diagnostic-agent",
            "success": False,
            "error": "Model not found",
        }
        mock_response = httpx.Response(200, json=error_response)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            with pytest.raises(AgentError, match="Model not found"):
                await client.run("Check hosts")


class TestRemoteAgentClientHealthz:
    """Tests for RemoteAgentClient.healthz()."""

    @pytest.mark.asyncio
    async def test_healthy_returns_true(self) -> None:
        """Test that a 200 response returns True."""
        mock_response = httpx.Response(200, json={"status": "ready"})
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            assert await client.healthz() is True

    @pytest.mark.asyncio
    async def test_unhealthy_returns_false(self) -> None:
        """Test that a non-200 response returns False."""
        mock_response = httpx.Response(503, json={"status": "initializing"})
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            assert await client.healthz() is False

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self) -> None:
        """Test that a connection error returns False."""
        with patch.object(
            httpx.AsyncClient,
            "get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            client = RemoteAgentClient("http://agent:8080")
            assert await client.healthz() is False


class TestRemoteAgentClientAsync:
    """Tests for RemoteAgentClient async methods."""

    @pytest.mark.asyncio
    async def test_run_async_returns_run_id(self) -> None:
        """Test that run_async returns a run_id from 202 response."""
        mock_response = httpx.Response(
            202, json={"run_id": "run-abc", "status": "running"}
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            run_id = await client.run_async("Check hosts")
        assert run_id == "run-abc"

    @pytest.mark.asyncio
    async def test_poll_run_returns_state(self) -> None:
        """Test that poll_run returns a RunState."""
        state_data = RunState(
            run_id="run-abc",
            status=RunStatus.COMPLETED,
            result=AgentRunResponse(
                output={"summary": "done"},
                output_type="DiagnosticReport",
                usage={"input_tokens": 10, "output_tokens": 20},
                agent_name="diag",
                success=True,
            ),
            created_at="2026-06-17T14:00:00+00:00",
        )
        mock_response = httpx.Response(200, json=state_data.model_dump(mode="json"))
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            state = await client.poll_run("run-abc")
        assert state.status == RunStatus.COMPLETED
        assert state.result.success is True

    @pytest.mark.asyncio
    async def test_poll_run_not_found_raises(self) -> None:
        """Test that polling a nonexistent run raises AgentError."""
        mock_response = httpx.Response(404, json={"detail": "not found"})
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_response
        ):
            client = RemoteAgentClient("http://agent:8080")
            with pytest.raises(AgentError, match="not found"):
                await client.poll_run("nonexistent")
