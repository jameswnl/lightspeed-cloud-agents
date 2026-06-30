"""Unit tests for Temporal workflow API endpoints (TDD)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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
        self,
        client: TestClient,
        mock_client: Any,
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
        self,
        client: TestClient,
        mock_client: Any,
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

    def test_duplicate_workflow_id_returns_409(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Duplicate workflow_id submission returns 409 Conflict."""
        from temporalio.service import RPCError, RPCStatusCode

        exc = RPCError(
            message="Workflow execution already started",
            status=RPCStatusCode.ALREADY_EXISTS,
            raw_grpc_status=None,
        )
        mock_client.start_workflow = AsyncMock(side_effect=exc)

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
        assert response.status_code == 409

    def test_mcp_servers_propagated_to_workflow_input(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """MCP servers from request are passed to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "mcp_servers": [{"name": "sn", "url": "http://mcp.local/sse"}],
            },
        )
        assert response.status_code == 202
        call_args = mock_client.start_workflow.call_args
        workflow_input = call_args[0][1]  # second positional arg
        assert workflow_input.mcp_servers is not None
        assert len(workflow_input.mcp_servers) == 1

    def test_caller_supplied_workflow_id_used(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Caller-supplied workflow_id is used instead of generated one."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "workflow_id": "wf-my-custom-id",
            },
        )
        assert response.status_code == 202
        assert response.json()["workflow_id"] == "wf-my-custom-id"

    def test_workflow_started_audit_event_emitted(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Starting a workflow emits workflow_started audit event."""
        mock_emit = mocker.patch("agents.workflow.temporal_api.emit_audit")
        client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "diag"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
            },
        )
        started_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "workflow_started"
        ]
        assert len(started_calls) == 1
        assert started_calls[0][1]["details"]["definition_name"] == "diag"


class TestApproveWorkflow:
    """Tests for POST /v1/workflows/{id}/approve."""

    def test_approve_returns_200(
        self,
        client: TestClient,
        mock_client: Any,
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

    def test_approval_emits_audit_event(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Approval emits step_approved audit event."""
        mock_emit = mocker.patch("agents.workflow.temporal_api.emit_audit")
        client.post(
            "/v1/workflows/wf-test-1/approve",
            json={"step_name": "approve-step", "decision": "approved"},
        )
        approved_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "step_approved"
        ]
        assert len(approved_calls) == 1
        assert approved_calls[0][1]["step_name"] == "approve-step"

    def test_denial_emits_audit_event(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Denial emits step_denied audit event."""
        mock_emit = mocker.patch("agents.workflow.temporal_api.emit_audit")
        client.post(
            "/v1/workflows/wf-test-1/approve",
            json={"step_name": "approve-step", "decision": "denied"},
        )
        denied_calls = [
            c for c in mock_emit.call_args_list
            if c[1].get("event_type") == "step_denied"
        ]
        assert len(denied_calls) == 1

    def test_approve_with_option_id(
        self,
        client: TestClient,
        mock_client: Any,
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
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Query returns workflow status."""
        response = client.get("/v1/workflows/wf-test-1")
        assert response.status_code == 200


class TestCancelWorkflow:
    """Tests for POST /v1/workflows/{id}/cancel."""

    def test_cancel_returns_200(
        self,
        client: TestClient,
        mock_client: Any,
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
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Advisory flag from request is passed to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "advisory": True,
            },
        )
        assert response.status_code == 202
        call_args = mock_client.start_workflow.call_args
        wf_input = call_args[0][1]
        assert wf_input.advisory is True

    def test_advisory_defaults_false(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Advisory defaults to False when not set."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
            },
        )
        assert response.status_code == 202
        call_args = mock_client.start_workflow.call_args
        wf_input = call_args[0][1]
        assert wf_input.advisory is False


class TestDefinitionManagement:
    """Tests for definition submission and retrieval."""

    def test_post_definition(self, mocker: MockerFixture) -> None:
        """POST /definitions creates a definition."""
        from agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        store = DefinitionStore()
        app = FastAPI()
        router = build_temporal_router(mock_temporal, definition_store=store)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.post(
            "/v1/workflows/definitions",
            json={
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "my-wf"},
                "spec": {
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "agent": "diag",
                            "prompt": "test",
                            "output_key": "r1",
                            "spawn": "pre-deployed",
                        },
                    ]
                },
            },
        )
        assert response.status_code == 201
        assert response.json()["name"] == "my-wf"

    def test_get_definition_by_name(self, mocker: MockerFixture) -> None:
        """GET /definitions/{name} returns a stored definition."""
        from agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        store = DefinitionStore()
        app = FastAPI()
        router = build_temporal_router(mock_temporal, definition_store=store)
        app.include_router(router)
        test_client = TestClient(app)

        test_client.post(
            "/v1/workflows/definitions",
            json={
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "fetch-wf"},
                "spec": {
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "agent": "diag",
                            "prompt": "test",
                            "output_key": "r1",
                            "spawn": "pre-deployed",
                        },
                    ]
                },
            },
        )

        response = test_client.get("/v1/workflows/definitions/fetch-wf")
        assert response.status_code == 200
        assert response.json()["name"] == "fetch-wf"

    def test_get_definition_not_found(self, mocker: MockerFixture) -> None:
        """GET /definitions/{name} returns 404 for unknown name."""
        from agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        store = DefinitionStore()
        app = FastAPI()
        router = build_temporal_router(mock_temporal, definition_store=store)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/definitions/missing")
        assert response.status_code == 404


class TestConfigPropagation:
    """Tests for notifier/escalation config propagation."""

    def test_notifier_config_propagated(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """notifier_config flows from request to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "notifier_config": {"type": "slack", "config_ref": "my-channel"},
            },
        )
        assert response.status_code == 202
        wf_input = mock_client.start_workflow.call_args[0][1]
        assert wf_input.notifier_config == {"type": "slack", "config_ref": "my-channel"}

    def test_escalation_config_propagated(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """escalation_config flows from request to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": []},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "escalation_config": {"type": "webhook", "config_ref": "ops-endpoint"},
            },
        )
        assert response.status_code == 202
        wf_input = mock_client.start_workflow.call_args[0][1]
        assert wf_input.escalation_config == {
            "type": "webhook",
            "config_ref": "ops-endpoint",
        }


class TestAuthEnforcement:
    """Tests that auth dependency is enforced when provided."""

    def test_unauthenticated_request_rejected(
        self,
        mocker: MockerFixture,
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
            mock_temporal,
            auth_dependency=reject_unauthenticated,
        )
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/v1/workflows/run", json={}).status_code == 401
        assert client.post("/v1/workflows/wf-1/approve", json={}).status_code == 401
        assert client.get("/v1/workflows/wf-1").status_code == 401
        assert client.post("/v1/workflows/wf-1/cancel").status_code == 401
