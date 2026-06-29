"""Unit tests for Temporal workflow API endpoints (TDD)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from agents.workflow.temporal_api import build_temporal_router


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


@pytest.fixture
def app(mock_client: Any) -> FastAPI:
    """Create a test FastAPI app with temporal router."""
    app = FastAPI()
    router = build_temporal_router(mock_client)
    app.include_router(router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(app)


class TestRunWorkflow:
    """Tests for POST /v1/workflows/run."""

    def test_start_workflow_returns_202(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Starting a workflow returns 202 with workflow_id."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "test-wf"},
                    "spec": {"steps": []},
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

    def test_start_workflow_calls_temporal(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Starting a workflow calls Temporal client."""
        client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "test-wf"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "key",
                },
            },
        )
        mock_client.start_workflow.assert_called_once()


class TestApproveWorkflow:
    """Tests for POST /v1/workflows/{id}/approve."""

    def test_approve_returns_200(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Sending approval returns 200."""
        response = client.post(
            "/v1/workflows/wf-test-1/approve",
            json={
                "step_name": "approve",
                "decision": "approved",
            },
        )
        assert response.status_code == 200

    def test_approve_with_option_id(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Approval with selected_option_id passes through."""
        response = client.post(
            "/v1/workflows/wf-test-1/approve",
            json={
                "step_name": "approve",
                "decision": "approved",
                "selected_option_id": "opt-2",
            },
        )
        assert response.status_code == 200
        handle = mock_client.get_workflow_handle.return_value
        handle.signal.assert_called_once()


class TestGetWorkflowStatus:
    """Tests for GET /v1/workflows/{id}."""

    def test_get_status_returns_200(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Query returns workflow status."""
        response = client.get("/v1/workflows/wf-test-1")
        assert response.status_code == 200


class TestCancelWorkflow:
    """Tests for POST /v1/workflows/{id}/cancel."""

    def test_cancel_returns_200(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Cancel returns 200."""
        response = client.post("/v1/workflows/wf-test-1/cancel")
        assert response.status_code == 200
        handle = mock_client.get_workflow_handle.return_value
        handle.cancel.assert_called_once()


class TestDefinitionRoutes:
    """Tests for definition management routes."""

    def test_get_definitions_returns_list(self, mocker: MockerFixture) -> None:
        """GET /definitions returns a list, not workflow status."""
        from agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()

        app = FastAPI()
        store = DefinitionStore()
        router = build_temporal_router(mock_temporal, definition_store=store)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/definitions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestAdvisoryPropagation:
    """Tests for advisory flag propagation through the API."""

    def test_advisory_from_request(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Advisory flag from request is passed to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1", "kind": "AgentWorkflow",
                    "metadata": {"name": "t"}, "spec": {"steps": []},
                },
                "provider": {"name": "openai", "model": "gpt-4",
                             "credentials_secret": "k"},
                "advisory": True,
            },
        )
        assert response.status_code == 202
        call_args = mock_client.start_workflow.call_args
        wf_input = call_args[0][1]
        assert wf_input.advisory is True

    def test_advisory_defaults_false(
        self, client: TestClient, mock_client: Any,
    ) -> None:
        """Advisory defaults to False when not set."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1", "kind": "AgentWorkflow",
                    "metadata": {"name": "t"}, "spec": {"steps": []},
                },
                "provider": {"name": "openai", "model": "gpt-4",
                             "credentials_secret": "k"},
            },
        )
        assert response.status_code == 202
        call_args = mock_client.start_workflow.call_args
        wf_input = call_args[0][1]
        assert wf_input.advisory is False


class TestAuthEnforcement:
    """Tests that auth dependency is enforced when provided."""

    def test_unauthenticated_request_rejected(
        self, mocker: MockerFixture,
    ) -> None:
        """Requests without auth are rejected when auth_dependency is set."""
        def reject_unauthenticated():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal, auth_dependency=reject_unauthenticated,
        )
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/v1/workflows/run", json={}).status_code == 401
        assert client.post("/v1/workflows/wf-1/approve", json={}).status_code == 401
        assert client.get("/v1/workflows/wf-1").status_code == 401
        assert client.post("/v1/workflows/wf-1/cancel").status_code == 401
