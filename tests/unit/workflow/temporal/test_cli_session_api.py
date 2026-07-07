"""Unit tests for CLI session status API endpoints (T15 Phase 2, Task 3)."""

from __future__ import annotations

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
