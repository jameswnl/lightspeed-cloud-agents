"""Tests for Chat API routes (Fix 6).

Covers:
- POST /chat — create a new conversation
- POST /chat/{conversation_id}/message — send a message
- GET /chat/{conversation_id}/history — get conversation history
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.workflow.executor.step.base import StepResult
from cloud_agents.workflow.executor.step.conversation import ConversationMessage


def _build_test_app(
    mock_runner: AsyncMock,
) -> FastAPI:
    """Build a minimal FastAPI app with the chat router for testing."""
    from cloud_agents.workflow.executor.chat.api import build_chat_router

    app = FastAPI()
    router = build_chat_router(runner=mock_runner)
    app.include_router(router, prefix="/v1")
    return app


class TestCreateConversation:
    """Tests for POST /v1/chat — create a new conversation."""

    def test_create_conversation(self) -> None:
        """POST /chat creates a conversation and returns ID."""
        mock_runner = AsyncMock()
        mock_runner.start = AsyncMock(return_value="chat-abc123")

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/chat", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == "chat-abc123"

    def test_create_conversation_with_user_id(self) -> None:
        """POST /chat passes user_id to runner.start()."""
        mock_runner = AsyncMock()
        mock_runner.start = AsyncMock(return_value="chat-xyz")

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/chat", json={"user_id": "alice"})
        assert response.status_code == 200

        call_args = mock_runner.start.call_args[0][0]
        assert call_args.get("user_id") == "alice"


class TestSendMessage:
    """Tests for POST /v1/chat/{conversation_id}/message."""

    def test_send_message_success(self) -> None:
        """POST /chat/{id}/message returns step result."""
        mock_runner = AsyncMock()
        mock_runner.send_message = AsyncMock(
            return_value=StepResult(
                status="completed",
                output={"response": "Hello!"},
                input_tokens=10,
                output_tokens=5,
                duration_ms=100,
            )
        )

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/chat/conv-1/message",
            json={"prompt": "Hi there"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["output"] == {"response": "Hello!"}

    def test_send_message_missing_prompt(self) -> None:
        """POST /chat/{id}/message returns 400 when prompt is missing."""
        mock_runner = AsyncMock()

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/chat/conv-1/message",
            json={},
        )
        assert response.status_code == 400

    def test_send_message_failed_result(self) -> None:
        """POST /chat/{id}/message returns failed status."""
        mock_runner = AsyncMock()
        mock_runner.send_message = AsyncMock(
            return_value=StepResult(
                status="failed",
                error="LLM timeout",
            )
        )

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/chat/conv-1/message",
            json={"prompt": "test"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "LLM timeout"

    def test_send_message_terminal_workflow(self) -> None:
        """POST /chat/{id}/message returns 409 for terminal conversation."""
        mock_runner = AsyncMock()
        mock_runner.send_message = AsyncMock(
            side_effect=RuntimeError("Conversation 'conv-1' is cancelled")
        )

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/chat/conv-1/message",
            json={"prompt": "test"},
        )
        assert response.status_code == 409


class TestGetHistory:
    """Tests for GET /v1/chat/{conversation_id}/history."""

    def test_get_history_success(self) -> None:
        """GET /chat/{id}/history returns conversation messages."""
        from datetime import UTC, datetime

        mock_runner = AsyncMock()
        mock_runner.get_history = AsyncMock(
            return_value=[
                ConversationMessage(
                    role="user",
                    content="Hello",
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                ConversationMessage(
                    role="assistant",
                    content="Hi!",
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/chat/conv-1/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "Hello"
        assert data["messages"][1]["role"] == "assistant"
        assert data["messages"][1]["content"] == "Hi!"

    def test_get_history_empty(self) -> None:
        """GET /chat/{id}/history returns empty list for new conversation."""
        mock_runner = AsyncMock()
        mock_runner.get_history = AsyncMock(return_value=[])

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/v1/chat/conv-1/history")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []

    def test_get_history_respects_limit_param(self) -> None:
        """GET /chat/{id}/history passes limit query parameter."""
        mock_runner = AsyncMock()
        mock_runner.get_history = AsyncMock(return_value=[])

        app = _build_test_app(mock_runner)
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/v1/chat/conv-1/history?limit=5")

        mock_runner.get_history.assert_called_once_with("conv-1", limit=5)
