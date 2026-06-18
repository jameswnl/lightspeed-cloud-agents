"""Unit tests for the agent runtime HTTP server."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from agents.models import AgentRunRequest, AgentRunResponse
from agents.runtime.server import create_app


@pytest.fixture(name="mock_agent_runner")
def mock_agent_runner_fixture() -> AsyncMock:
    """Create a mock agent runner that returns a valid AgentRunResponse."""
    runner = AsyncMock()
    runner.return_value = AgentRunResponse(
        output={"summary": "All clear", "issues_found": [], "actions_taken": [], "cluster_healthy": True},
        output_type="DiagnosticReport",
        schema_version="v1",
        usage={"input_tokens": 100, "output_tokens": 200},
        agent_name="test-agent",
        success=True,
    )
    return runner


@pytest.fixture(name="app_client")
def app_client_fixture(mock_agent_runner: AsyncMock) -> TestClient:
    """Create a TestClient with a mocked agent runner."""
    app = create_app(agent_runner=mock_agent_runner, agent_name="test-agent")
    return TestClient(app)


class TestHealthz:
    """Tests for the /healthz endpoint."""

    def test_healthz_returns_ready(self, app_client: TestClient) -> None:
        """Test that /healthz returns 200 with ready status."""
        resp = app_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_healthz_includes_agent_name(self, app_client: TestClient) -> None:
        """Test that /healthz includes the agent name."""
        resp = app_client.get("/healthz")
        assert resp.json()["agent_name"] == "test-agent"


class TestRunEndpoint:
    """Tests for the /v1/run endpoint."""

    def test_run_with_valid_prompt(
        self, app_client: TestClient, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that /v1/run accepts a valid prompt and returns AgentRunResponse."""
        resp = app_client.post(
            "/v1/run",
            json={"prompt": "Check all hosts"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["agent_name"] == "test-agent"
        assert body["output_type"] == "DiagnosticReport"
        assert body["schema_version"] == "v1"
        mock_agent_runner.assert_called_once()

    def test_run_with_context(
        self, app_client: TestClient, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that /v1/run passes context through."""
        resp = app_client.post(
            "/v1/run",
            json={
                "prompt": "Investigate web-02",
                "context": {"correlation_id": "test-123"},
            },
        )
        assert resp.status_code == 200
        call_args = mock_agent_runner.call_args
        request_arg = call_args[0][0]
        assert isinstance(request_arg, AgentRunRequest)
        assert request_arg.context["correlation_id"] == "test-123"

    def test_run_with_empty_prompt_returns_422(
        self, app_client: TestClient
    ) -> None:
        """Test that /v1/run rejects an empty prompt."""
        resp = app_client.post(
            "/v1/run",
            json={"prompt": ""},
        )
        assert resp.status_code == 422

    def test_run_with_missing_prompt_returns_422(
        self, app_client: TestClient
    ) -> None:
        """Test that /v1/run rejects a request with no prompt field."""
        resp = app_client.post(
            "/v1/run",
            json={},
        )
        assert resp.status_code == 422

    def test_run_agent_failure_returns_500(
        self, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that /v1/run returns 500 when the agent raises an exception."""
        mock_agent_runner.side_effect = RuntimeError("LLM connection failed")
        app = create_app(agent_runner=mock_agent_runner, agent_name="test-agent")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
        )
        assert resp.status_code == 500
        assert "LLM connection failed" in resp.json()["detail"]

    def test_run_returns_error_response_on_agent_error(
        self, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that /v1/run returns a structured error response."""
        mock_agent_runner.side_effect = RuntimeError("Model not found")
        app = create_app(agent_runner=mock_agent_runner, agent_name="fail-agent")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/run",
            json={"prompt": "Do something"},
        )
        assert resp.status_code == 500
        body = resp.json()
        assert "Model not found" in body["detail"]
