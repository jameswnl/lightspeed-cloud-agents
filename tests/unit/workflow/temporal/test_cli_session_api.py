"""Unit tests for CLI session API endpoints (T15 Phase 2 + Phase 3)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from cloud_agents.workflow.cli_session import (
    CLISessionInfo,
    CLISessionLauncher,
    CLISessionStatus,
)
from cloud_agents.workflow.temporal_api import build_temporal_router


@pytest.fixture
def mock_temporal(mocker: MockerFixture) -> Any:
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


@pytest.fixture
def launcher() -> CLISessionLauncher:
    """Create a CLISessionLauncher instance."""
    return CLISessionLauncher()


@pytest.fixture
def spawner() -> AsyncMock:
    """Create a mock spawner."""
    mock = AsyncMock()
    mock.spawn = AsyncMock(return_value="http://cli-agent:8080")
    mock.destroy = AsyncMock()
    return mock


@pytest.fixture
def app(mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock) -> FastAPI:
    """Create a test FastAPI app with CLI session router."""
    app = FastAPI()
    router = build_temporal_router(
        mock_temporal,
        cli_session_launcher=launcher,
        cli_session_spawner=spawner,
    )
    app.include_router(router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app)


async def _launch_session(
    launcher: CLISessionLauncher, spawner: AsyncMock, workflow_id: str = "wf-test-1"
) -> str:
    """Helper to launch a session for testing."""
    with patch("cloud_agents.workflow.cli_session.emit_audit"):
        return await launcher.launch(
            spawner=spawner,
            context_markdown="# Test context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id=workflow_id,
        )


class TestListCLISessions:
    """Tests for GET /v1/cli-sessions."""

    def test_list_empty_returns_200(self, client: TestClient) -> None:
        """List with no sessions returns 200 with empty list."""
        response = client.get("/v1/cli-sessions")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_active_sessions(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """List returns all active sessions."""
        await _launch_session(launcher, spawner, "wf-1")
        await _launch_session(launcher, spawner, "wf-2")

        response = client.get("/v1/cli-sessions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        workflow_ids = {s["workflow_id"] for s in data}
        assert workflow_ids == {"wf-1", "wf-2"}

    @pytest.mark.asyncio
    async def test_list_includes_session_fields(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Listed sessions include all expected fields."""
        await _launch_session(launcher, spawner)

        response = client.get("/v1/cli-sessions")
        data = response.json()
        session = data[0]
        assert "session_id" in session
        assert "agent_name" in session
        assert "workflow_id" in session
        assert "started_at" in session
        assert "status" in session
        assert session["status"] == "running"


class TestGetCLISession:
    """Tests for GET /v1/cli-sessions/{id}."""

    @pytest.mark.asyncio
    async def test_get_existing_session_returns_200(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Get existing session returns 200 with session info."""
        session_id = await _launch_session(launcher, spawner)

        response = client.get(f"/v1/cli-sessions/{session_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert data["status"] == "running"

    def test_get_nonexistent_session_returns_404(
        self, client: TestClient
    ) -> None:
        """Get nonexistent session returns 404."""
        response = client.get("/v1/cli-sessions/nonexistent")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_returns_workflow_id(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Get returns the associated workflow ID."""
        session_id = await _launch_session(launcher, spawner, "wf-check")

        response = client.get(f"/v1/cli-sessions/{session_id}")
        data = response.json()
        assert data["workflow_id"] == "wf-check"


class TestDeleteCLISession:
    """Tests for DELETE /v1/cli-sessions/{id}."""

    @pytest.mark.asyncio
    async def test_delete_session_returns_200(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Delete existing session returns 200."""
        session_id = await _launch_session(launcher, spawner)

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            response = client.delete(f"/v1/cli-sessions/{session_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "terminated"

    @pytest.mark.asyncio
    async def test_delete_calls_spawner_destroy(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Delete calls spawner.destroy() on the session container."""
        session_id = await _launch_session(launcher, spawner)

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            client.delete(f"/v1/cli-sessions/{session_id}")
        spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_updates_session_status(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Delete updates session status to terminated."""
        session_id = await _launch_session(launcher, spawner)

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            client.delete(f"/v1/cli-sessions/{session_id}")

        info = launcher.get_status(session_id)
        assert info is not None
        assert info.status == CLISessionStatus.TERMINATED

    def test_delete_nonexistent_session_returns_404(
        self, client: TestClient
    ) -> None:
        """Delete nonexistent session returns 404."""
        response = client.delete("/v1/cli-sessions/nonexistent")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_emits_audit_event(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Delete emits cli_session_terminated audit event."""
        session_id = await _launch_session(launcher, spawner)

        mock_emit = mocker.patch("cloud_agents.workflow.cli_session.emit_audit")
        client.delete(f"/v1/cli-sessions/{session_id}")

        terminated_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "cli_session_terminated"
        ]
        assert len(terminated_calls) == 1


class TestCLISessionAuthz:
    """Tests for RBAC on CLI session endpoints."""

    def _make_deny_authorizer(self) -> Any:
        """Create an authorizer that denies all requests."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            CallerIdentity,
            WorkflowAction,
            WorkflowAuthorizer,
            WorkflowResource,
        )

        class DenyAllAuthorizer(WorkflowAuthorizer):
            """Authorizer that denies all actions for testing."""

            async def authorize(
                self,
                identity: CallerIdentity,
                action: WorkflowAction,
                resource: WorkflowResource,
            ) -> AuthzDecision:
                """Deny all actions."""
                return AuthzDecision(allowed=False, reason="denied by test")

        return DenyAllAuthorizer()

    def test_list_denied_returns_403(
        self, mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock
    ) -> None:
        """List with deny-all authorizer returns 403."""
        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            authorizer=self._make_deny_authorizer(),
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/cli-sessions")
        assert response.status_code == 403

    def test_get_session_denied_returns_403(
        self, mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock
    ) -> None:
        """Get session with deny-all authorizer returns 403."""
        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            authorizer=self._make_deny_authorizer(),
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/cli-sessions/some-id")
        assert response.status_code == 403

    def test_delete_denied_returns_403(
        self, mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock
    ) -> None:
        """Delete with deny-all authorizer returns 403."""
        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            authorizer=self._make_deny_authorizer(),
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.delete("/v1/cli-sessions/some-id")
        assert response.status_code == 403


class TestCLISessionEndpointsWithoutLauncher:
    """Tests that session endpoints are not registered when launcher is not provided."""

    def test_no_launcher_no_session_routes(self, mock_temporal: Any) -> None:
        """Without cli_session_launcher, session endpoints are not registered."""
        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/cli-sessions")
        # Should be 404 (route not found) or 405 (method not allowed)
        assert response.status_code in (404, 405)


class TestWorkflowRoutesUnaffectedByLauncher:
    """Tests that workflow endpoints still work when CLI session launcher is provided."""

    def test_workflow_run_still_works_with_launcher(
        self,
        mock_temporal: Any,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST /v1/workflows/run still returns 202 when launcher is configured."""
        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.post(
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
        assert response.status_code == 202
        assert "workflow_id" in response.json()

    def test_workflow_status_still_works_with_launcher(
        self,
        mock_temporal: Any,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """GET /v1/workflows/{id} still returns 200 when launcher is configured."""
        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-test-1")
        assert response.status_code == 200


class TestPostCLISessionMessage:
    """Tests for POST /v1/cli-sessions/{id}/messages."""

    @pytest.mark.asyncio
    async def test_send_message_returns_200(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message to existing session returns 200."""
        session_id = await _launch_session(launcher, spawner)

        spawner.read_file = AsyncMock(side_effect=FileNotFoundError("no file"))
        spawner.write_file = AsyncMock()

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            response = client.post(
                f"/v1/cli-sessions/{session_id}/messages",
                json={"message": "Hello agent!"},
            )
        assert response.status_code == 200
        assert response.json()["status"] == "sent"

    @pytest.mark.asyncio
    async def test_send_message_calls_launcher(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message calls launcher.send_message with correct args."""
        session_id = await _launch_session(launcher, spawner)

        spawner.read_file = AsyncMock(side_effect=FileNotFoundError("no file"))
        spawner.write_file = AsyncMock()

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            client.post(
                f"/v1/cli-sessions/{session_id}/messages",
                json={"message": "Test message"},
            )

        spawner.write_file.assert_called_once()

    def test_send_message_nonexistent_session_returns_404(
        self, client: TestClient
    ) -> None:
        """POST to nonexistent session returns 404."""
        response = client.post(
            "/v1/cli-sessions/nonexistent/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 404

    def test_send_message_denied_returns_403(
        self, mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock
    ) -> None:
        """POST message with deny-all authorizer returns 403."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            CallerIdentity,
            WorkflowAction,
            WorkflowAuthorizer,
            WorkflowResource,
        )

        class DenyAllAuthorizer(WorkflowAuthorizer):
            async def authorize(self, identity, action, resource):
                return AuthzDecision(allowed=False, reason="denied")

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            authorizer=DenyAllAuthorizer(),
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post(
            "/v1/cli-sessions/some-id/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 403


class TestSendMessageStatusGuard:
    """Tests for 409 Conflict when sending to TERMINATED/FAILED sessions."""

    @pytest.mark.asyncio
    async def test_send_message_to_terminated_session_returns_409(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message to terminated session returns 409 Conflict."""
        session_id = await _launch_session(launcher, spawner)

        # Terminate the session
        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            await launcher.terminate(session_id, spawner)

        response = client.post(
            f"/v1/cli-sessions/{session_id}/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 409
        assert "terminated" in response.json()["detail"].lower() or \
               "cannot send" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_send_message_to_failed_session_returns_409(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message to failed session returns 409 Conflict."""
        session_id = await _launch_session(launcher, spawner)

        # Simulate a failed session by setting status directly
        launcher._sessions[session_id].status = CLISessionStatus.FAILED

        response = client.post(
            f"/v1/cli-sessions/{session_id}/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_send_message_to_completed_session_returns_409(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message to completed session returns 409 Conflict."""
        session_id = await _launch_session(launcher, spawner)

        # Simulate completed session
        launcher._sessions[session_id].status = CLISessionStatus.COMPLETED

        response = client.post(
            f"/v1/cli-sessions/{session_id}/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_send_message_to_running_session_succeeds(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """POST message to running session returns 200 (no guard)."""
        session_id = await _launch_session(launcher, spawner)

        spawner.read_file = AsyncMock(side_effect=FileNotFoundError("no file"))
        spawner.write_file = AsyncMock()

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            response = client.post(
                f"/v1/cli-sessions/{session_id}/messages",
                json={"message": "hello"},
            )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_409_detail_includes_session_status(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """409 response detail includes the current session status."""
        session_id = await _launch_session(launcher, spawner)
        launcher._sessions[session_id].status = CLISessionStatus.TERMINATED

        response = client.post(
            f"/v1/cli-sessions/{session_id}/messages",
            json={"message": "hello"},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "terminated" in detail.lower()


class TestMessageSizeLimits:
    """Tests for message size limits on SendMessageRequest."""

    def test_message_exceeding_64kb_returns_422(
        self, client: TestClient
    ) -> None:
        """Message larger than 64KB returns 422."""
        # 64KB = 65536 bytes; send slightly over
        large_message = "A" * 65537

        response = client.post(
            "/v1/cli-sessions/any-id/messages",
            json={"message": large_message},
        )
        # Pydantic validation returns 422 for field validation errors
        assert response.status_code == 422

    def test_message_exactly_64kb_succeeds_validation(
        self,
        client: TestClient,
    ) -> None:
        """Message at exactly 64KB passes validation (returns 404 for missing session)."""
        exact_message = "A" * 65536

        response = client.post(
            "/v1/cli-sessions/nonexistent/messages",
            json={"message": exact_message},
        )
        # Should pass validation but fail on session lookup
        assert response.status_code == 404

    def test_empty_message_returns_422(
        self, client: TestClient
    ) -> None:
        """Empty message returns 422."""
        response = client.post(
            "/v1/cli-sessions/any-id/messages",
            json={"message": ""},
        )
        assert response.status_code == 422


class TestMessageInputValidation:
    """Tests for message content sanitization and validation."""

    def test_control_characters_stripped(
        self, client: TestClient,
    ) -> None:
        """Control characters are stripped from message content."""
        # Message with control characters (except \n, \r, \t which are allowed)
        msg_with_controls = "Hello\x00World\x01Test\x7f"

        response = client.post(
            "/v1/cli-sessions/nonexistent/messages",
            json={"message": msg_with_controls},
        )
        # Should pass validation (404 for missing session, not 422)
        assert response.status_code == 404

    def test_newlines_and_tabs_preserved(
        self, client: TestClient,
    ) -> None:
        """Newlines, carriage returns, and tabs are preserved."""
        msg = "Hello\nWorld\r\nTest\tTab"

        response = client.post(
            "/v1/cli-sessions/nonexistent/messages",
            json={"message": msg},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_sanitized_message_delivered_to_session(
        self,
        client: TestClient,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
    ) -> None:
        """Control characters are stripped before delivering to session."""
        session_id = await _launch_session(launcher, spawner)

        spawner.read_file = AsyncMock(side_effect=FileNotFoundError("no file"))
        spawner.write_file = AsyncMock()

        with patch("cloud_agents.workflow.cli_session.emit_audit"):
            response = client.post(
                f"/v1/cli-sessions/{session_id}/messages",
                json={"message": "Hello\x00World"},
            )
        assert response.status_code == 200

        # The message written to file should have control chars stripped
        call_args = spawner.write_file.call_args
        written_content = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("content", "")
        assert "\x00" not in written_content
        assert "HelloWorld" in written_content

    def test_whitespace_only_message_returns_422(
        self, client: TestClient,
    ) -> None:
        """Message with only whitespace returns 422."""
        response = client.post(
            "/v1/cli-sessions/any-id/messages",
            json={"message": "   \n\t  "},
        )
        assert response.status_code == 422

    def test_valid_unicode_message_accepted(
        self, client: TestClient,
    ) -> None:
        """Valid unicode message with multi-byte chars passes validation."""
        # Include actual multi-byte characters (emoji, CJK)
        msg = "Hello 世界! \U0001f680 Multi-byte test"

        response = client.post(
            "/v1/cli-sessions/nonexistent/messages",
            json={"message": msg},
        )
        assert response.status_code == 404  # passes validation, fails on session lookup

    def test_multibyte_message_over_64kb_in_bytes_returns_422(
        self, client: TestClient,
    ) -> None:
        """Multi-byte message under 64KB char-count but over 64KB byte-count returns 422."""
        # é is 2 bytes in UTF-8; 32769 copies = 65538 bytes > 64KB
        multibyte_msg = "é" * 32769

        assert len(multibyte_msg) == 32769  # char count under 64K
        assert len(multibyte_msg.encode("utf-8")) == 65538  # byte count over 64K

        response = client.post(
            "/v1/cli-sessions/any-id/messages",
            json={"message": multibyte_msg},
        )
        assert response.status_code == 422


class TestGetCLISessionOutput:
    """Tests for GET /v1/cli-sessions/{id}/output (SSE stream)."""

    @pytest.mark.asyncio
    async def test_output_stream_returns_sse(
        self,
        launcher: CLISessionLauncher,
        spawner: AsyncMock,
        mock_temporal: Any,
    ) -> None:
        """GET output returns SSE media type."""
        session_id = await _launch_session(launcher, spawner)

        # Make read_file return content then end
        spawner.read_file = AsyncMock(
            side_effect=[
                '{"event": "test"}\n',
                FileNotFoundError("done"),
            ]
        )

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get(f"/v1/cli-sessions/{session_id}/output")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert "data:" in response.text

    def test_output_nonexistent_session_returns_404(
        self, client: TestClient
    ) -> None:
        """GET output for nonexistent session returns 404."""
        response = client.get("/v1/cli-sessions/nonexistent/output")
        assert response.status_code == 404

    def test_output_denied_returns_403(
        self, mock_temporal: Any, launcher: CLISessionLauncher, spawner: AsyncMock
    ) -> None:
        """GET output with deny-all authorizer returns 403."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            WorkflowAuthorizer,
        )

        class DenyAllAuthorizer(WorkflowAuthorizer):
            async def authorize(self, identity, action, resource):
                return AuthzDecision(allowed=False, reason="denied")

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal,
            authorizer=DenyAllAuthorizer(),
            cli_session_launcher=launcher,
            cli_session_spawner=spawner,
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/cli-sessions/some-id/output")
        assert response.status_code == 403
