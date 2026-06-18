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


class TestAsyncRun:
    """Tests for the async /v1/run mode with Prefer: respond-async."""

    def test_async_submit_returns_202(
        self, app_client: TestClient
    ) -> None:
        """Test that Prefer: respond-async returns 202 with run_id."""
        resp = app_client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
            headers={"Prefer": "respond-async"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "run_id" in body
        assert body["status"] == "running"

    def test_sync_mode_without_prefer_header(
        self, app_client: TestClient
    ) -> None:
        """Test that without Prefer header, behavior is sync (200)."""
        resp = app_client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
        )
        assert resp.status_code == 200
        assert "success" in resp.json()

    def test_poll_running_run(
        self, app_client: TestClient
    ) -> None:
        """Test polling a running run returns status."""
        submit = app_client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
            headers={"Prefer": "respond-async"},
        )
        run_id = submit.json()["run_id"]
        poll = app_client.get(f"/v1/runs/{run_id}")
        assert poll.status_code == 200
        body = poll.json()
        assert body["run_id"] == run_id
        assert body["status"] in ("running", "completed")

    def test_poll_unknown_run_returns_404(
        self, app_client: TestClient
    ) -> None:
        """Test polling a nonexistent run returns 404."""
        resp = app_client.get("/v1/runs/nonexistent-id")
        assert resp.status_code == 404

    def test_poll_completed_run_has_result(
        self, app_client: TestClient
    ) -> None:
        """Test that a completed run returns the result when polled."""
        import time
        submit = app_client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
            headers={"Prefer": "respond-async"},
        )
        run_id = submit.json()["run_id"]
        for _ in range(20):
            poll = app_client.get(f"/v1/runs/{run_id}")
            body = poll.json()
            if body["status"] == "completed":
                break
            time.sleep(0.1)
        assert body["status"] == "completed", f"Run did not complete: {body}"
        assert body["result"] is not None
        assert body["result"]["success"] is True


class TestLivez:
    """Tests for the /livez liveness endpoint."""

    def test_livez_returns_200_when_healthy(
        self, app_client: TestClient
    ) -> None:
        """Test that /livez returns 200 by default."""
        resp = app_client.get("/livez")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    def test_livez_returns_503_when_stale(
        self, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that /livez returns 503 when heartbeat is stale."""
        import time
        app = create_app(
            agent_runner=mock_agent_runner,
            agent_name="test-agent",
            run_timeout=0.5,
        )
        app.state.last_heartbeat = time.monotonic() - 100
        client = TestClient(app)
        resp = client.get("/livez")
        assert resp.status_code == 503
        assert resp.json()["status"] == "stale"


class TestRunTimeout:
    """Tests for run timeout enforcement."""

    def test_run_within_timeout_succeeds(
        self, mock_agent_runner: AsyncMock
    ) -> None:
        """Test that a fast run completes normally."""
        app = create_app(
            agent_runner=mock_agent_runner,
            agent_name="test-agent",
            run_timeout=10.0,
        )
        client = TestClient(app)
        resp = client.post("/v1/run", json={"prompt": "Quick check"})
        assert resp.status_code == 200

    def test_run_exceeding_timeout_returns_500(self) -> None:
        """Test that a slow run is cancelled and returns 500."""
        import asyncio

        async def slow_runner(req: AgentRunRequest) -> AgentRunResponse:
            await asyncio.sleep(10)
            return AgentRunResponse(
                output={}, output_type="test",
                usage={}, agent_name="test", success=True,
            )

        app = create_app(
            agent_runner=slow_runner,
            agent_name="test-agent",
            run_timeout=0.1,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/run", json={"prompt": "Slow check"})
        assert resp.status_code == 500
        assert "timed out" in resp.json()["detail"].lower()


class TestCorrelationId:
    """Tests for correlation ID in responses."""

    def test_response_includes_correlation_id_header(
        self, app_client: TestClient
    ) -> None:
        """Test that responses include X-Correlation-ID header."""
        resp = app_client.post(
            "/v1/run",
            json={"prompt": "Check hosts"},
        )
        assert "x-correlation-id" in resp.headers

    def test_caller_provided_correlation_id_echoed(
        self, app_client: TestClient
    ) -> None:
        """Test that a valid caller correlation ID is echoed back."""
        resp = app_client.post(
            "/v1/run",
            json={
                "prompt": "Check hosts",
                "context": {"correlation_id": "my-request-123"},
            },
        )
        assert resp.headers["x-correlation-id"] == "my-request-123"

    def test_invalid_correlation_id_replaced(
        self, app_client: TestClient
    ) -> None:
        """Test that an invalid correlation ID is replaced with a UUID."""
        resp = app_client.post(
            "/v1/run",
            json={
                "prompt": "Check hosts",
                "context": {"correlation_id": "invalid;chars<>"},
            },
        )
        cid = resp.headers["x-correlation-id"]
        assert cid != "invalid;chars<>"
        import uuid
        uuid.UUID(cid)


class TestMetricsEndpoint:
    """Tests for the /metrics endpoint."""

    def test_metrics_returns_prometheus_format(
        self, app_client: TestClient
    ) -> None:
        """Test that /metrics returns Prometheus text format."""
        app_client.post("/v1/run", json={"prompt": "Check hosts"})
        resp = app_client.get("/metrics")
        assert resp.status_code == 200
        assert "ls_agent_runs_total" in resp.text
