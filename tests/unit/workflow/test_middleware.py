"""Unit tests for ContentSizeLimitMiddleware (TDD)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from cloud_agents.workflow.middleware import ContentSizeLimitMiddleware
from cloud_agents.workflow.temporal_api import build_temporal_router


@pytest.fixture
def mock_client(mocker: MockerFixture) -> Any:
    """Create a mock Temporal client."""
    client = mocker.MagicMock()
    handle = mocker.AsyncMock()
    handle.id = "wf-test-1"
    handle.query.return_value = mocker.MagicMock(
        model_dump=lambda: {"steps": {}, "events": []}
    )
    client.start_workflow = mocker.AsyncMock(return_value=handle)
    client.get_workflow_handle.return_value = handle
    return client


def _app_with_limit(mock_client: Any, limit: int = 1024) -> FastAPI:
    """Build a FastAPI app with the size limit middleware applied.

    Parameters:
        mock_client: Mock Temporal client for router construction.
        limit: Maximum request body size in bytes.

    Returns:
        FastAPI application with ContentSizeLimitMiddleware.
    """
    app = FastAPI()
    router = build_temporal_router(mock_client)
    app.include_router(router)
    app.add_middleware(ContentSizeLimitMiddleware, max_content_size=limit)
    return app


class TestContentSizeLimitMiddleware:
    """Tests for ContentSizeLimitMiddleware."""

    def test_oversized_content_length_returns_413(
        self, mock_client: Any
    ) -> None:
        """Request with Content-Length exceeding the limit returns 413."""
        app = _app_with_limit(mock_client, limit=1024)
        client = TestClient(app, raise_server_exceptions=False)

        # Build a payload larger than 1024 bytes
        oversized_body = "x" * 2048
        response = client.post(
            "/v1/workflows/run",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert "too large" in response.json()["detail"].lower()

    def test_oversized_chunked_body_returns_413(
        self, mock_client: Any
    ) -> None:
        """Chunked request exceeding the limit returns 413."""
        app = _app_with_limit(mock_client, limit=512)
        client = TestClient(app, raise_server_exceptions=False)

        # Send body larger than 512 bytes
        oversized_body = '{"data": "' + "a" * 600 + '"}'
        response = client.post(
            "/v1/workflows/run",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413

    def test_normal_payload_passes(self, mock_client: Any) -> None:
        """Request within the size limit passes through to the endpoint."""
        app = _app_with_limit(mock_client, limit=65536)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "test-wf"},
                    "spec": {
                        "steps": [
                            {
                                "name": "s1",
                                "type": "agent",
                                "output_key": "r1",
                                "prompt": "test",
                            }
                        ]
                    },
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "key",
                },
            },
        )
        # Should reach the actual endpoint (202 for a valid run request)
        assert response.status_code == 202

    def test_get_requests_not_affected(self, mock_client: Any) -> None:
        """GET requests bypass the body size check."""
        app = _app_with_limit(mock_client, limit=64)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/workflows/wf-test-1")
        # Should reach the actual endpoint, not be blocked by middleware
        assert response.status_code == 200

    def test_exact_limit_passes(self, mock_client: Any) -> None:
        """Request body exactly at the limit passes through."""
        limit = 2048
        app = _app_with_limit(mock_client, limit=limit)
        client = TestClient(app, raise_server_exceptions=False)

        # Create a body that is exactly `limit` bytes
        # The JSON serialization adds overhead, so we use raw content
        body = "x" * limit
        response = client.post(
            "/v1/workflows/run",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        # At exactly the limit, should NOT return 413
        # (it may return 422 for invalid JSON, which is fine — not 413)
        assert response.status_code != 413

    def test_413_response_includes_limit_in_detail(
        self, mock_client: Any
    ) -> None:
        """413 response body includes the configured limit."""
        limit = 256
        app = _app_with_limit(mock_client, limit=limit)
        client = TestClient(app, raise_server_exceptions=False)

        oversized_body = "x" * 512
        response = client.post(
            "/v1/workflows/run",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert str(limit) in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_chunked_body_counted_via_receive(self) -> None:
        """Byte counting via receive() catches oversized chunked bodies."""
        responses: list[dict] = []

        async def capture_send(message):
            responses.append(message)

        async def inner_app(scope, receive, send):
            # Consume the body like FastAPI would
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = ContentSizeLimitMiddleware(inner_app, max_content_size=50)

        # Simulate chunked transfer — no Content-Length header, body in receive()
        chunk = b"x" * 100
        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "http.request", "body": chunk, "more_body": False}
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/workflows/run",
            "headers": [],  # No Content-Length header
        }

        await middleware(scope, mock_receive, capture_send)
        status = next(r["status"] for r in responses if r["type"] == "http.response.start")
        assert status == 413

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self) -> None:
        """Non-HTTP scopes (e.g., WebSocket) pass through without checking."""
        app_called = False

        async def inner_app(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = ContentSizeLimitMiddleware(inner_app, max_content_size=10)
        await middleware({"type": "websocket"}, None, None)
        assert app_called
