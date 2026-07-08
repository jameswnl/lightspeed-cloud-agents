"""Unit tests for Temporal workflow API endpoints (TDD)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
        mock_emit = mocker.patch("cloud_agents.workflow.temporal_api.emit_audit")
        client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "diag"},
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
        mock_emit = mocker.patch("cloud_agents.workflow.temporal_api.emit_audit")
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
        mock_emit = mocker.patch("cloud_agents.workflow.temporal_api.emit_audit")
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
        from cloud_agents.workflow.definition_store import DefinitionStore

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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
        from cloud_agents.workflow.definition_store import DefinitionStore

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

    def test_post_definition_with_invalid_schema_returns_422(
        self, mocker: MockerFixture
    ) -> None:
        """POST /definitions with invalid output_schema returns 422."""
        from cloud_agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        store = DefinitionStore()
        app = FastAPI()
        router = build_temporal_router(mock_temporal, definition_store=store)
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post(
            "/v1/workflows/definitions",
            json={
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "bad-schema"},
                "spec": {
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "prompt": "test",
                            "output_key": "r1",
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    "things": {"type": "array"},
                                },
                            },
                        },
                    ]
                },
            },
        )
        assert response.status_code == 422
        body = response.json()
        assert "validation_errors" in body.get("detail", {})

    def test_get_definition_by_name(self, mocker: MockerFixture) -> None:
        """GET /definitions/{name} returns a stored definition."""
        from cloud_agents.workflow.definition_store import DefinitionStore

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
        from cloud_agents.workflow.definition_store import DefinitionStore

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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
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


class TestDeploymentConfigPropagation:
    """Tests for skills_image and sandbox_image propagation."""

    def test_skills_image_propagated(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """skills_image flows from request to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "skills_image": "quay.io/team/diagnostic-skills:v1",
                "skills_paths": ["/skills/diag"],
            },
        )
        assert response.status_code == 202
        wf_input = mock_client.start_workflow.call_args[0][1]
        assert wf_input.skills_image == "quay.io/team/diagnostic-skills:v1"
        assert wf_input.skills_paths == ["/skills/diag"]

    def test_custom_sandbox_image_propagated(
        self,
        client: TestClient,
        mock_client: Any,
    ) -> None:
        """Custom sandbox_image flows from request to WorkflowInput."""
        response = client.post(
            "/v1/workflows/run",
            json={
                "definition": {
                    "apiVersion": "v1",
                    "kind": "AgentWorkflow",
                    "metadata": {"name": "t"},
                    "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
                },
                "provider": {
                    "name": "openai",
                    "model": "gpt-4",
                    "credentials_secret": "k",
                },
                "sandbox_image": "quay.io/team/custom-agent:v2",
            },
        )
        assert response.status_code == 202
        wf_input = mock_client.start_workflow.call_args[0][1]
        assert wf_input.sandbox_image == "quay.io/team/custom-agent:v2"


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


# -- Helpers for authorization tests -------------------------------------------

VALID_RUN_PAYLOAD: dict[str, Any] = {
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
}


def _make_deny_all_authorizer():
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


def _build_deny_app(mock_temporal: Any, definition_store=None) -> FastAPI:
    """Build a FastAPI app with a deny-all authorizer."""
    app = FastAPI()
    router = build_temporal_router(
        mock_temporal,
        authorizer=_make_deny_all_authorizer(),
        definition_store=definition_store,
    )
    app.include_router(router)
    return app


class TestAuthorizationWiring:
    """Tests for authorization checks wired into all API endpoints."""

    def test_unauthorized_trigger_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Trigger with deny-all authorizer returns 403."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()

        app = _build_deny_app(mock_temporal)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 403
        assert "denied by test" in response.json()["detail"]

    def test_authorized_trigger_succeeds(
        self,
        mock_client: Any,
    ) -> None:
        """Trigger with NoopAuthorizer (default) succeeds -- backward compatible."""
        app = FastAPI()
        router = build_temporal_router(mock_client)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 202
        assert "workflow_id" in response.json()

    def test_unauthorized_approve_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Approve with deny-all authorizer returns 403."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        app = _build_deny_app(mock_temporal)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post(
            "/v1/workflows/wf-test-1/approve",
            json={"step_name": "s1", "decision": "approved"},
        )
        assert response.status_code == 403
        assert "denied by test" in response.json()["detail"]

    def test_unauthorized_view_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /{workflow_id} with deny-all authorizer returns 403."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        app = _build_deny_app(mock_temporal)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/workflows/wf-test-1")
        assert response.status_code == 403

    def test_unauthorized_cancel_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /{workflow_id}/cancel with deny-all authorizer returns 403."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        app = _build_deny_app(mock_temporal)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/wf-test-1/cancel")
        assert response.status_code == 403

    def test_unauthorized_handoff_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /{workflow_id}/handoff with deny-all authorizer returns 403."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        app = _build_deny_app(mock_temporal)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/workflows/wf-test-1/handoff")
        assert response.status_code == 403

    def test_unauthorized_definitions_get_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /definitions with deny-all authorizer returns 403."""
        from cloud_agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = DefinitionStore()

        app = _build_deny_app(mock_temporal, definition_store=store)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/workflows/definitions")
        assert response.status_code == 403

    def test_unauthorized_definitions_post_returns_403(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /definitions with deny-all authorizer returns 403."""
        from cloud_agents.workflow.definition_store import DefinitionStore

        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = mocker.AsyncMock()
        store = DefinitionStore()

        app = _build_deny_app(mock_temporal, definition_store=store)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post(
            "/v1/workflows/definitions",
            json={
                "apiVersion": "v1",
                "kind": "AgentWorkflow",
                "metadata": {"name": "test"},
                "spec": {
                    "steps": [
                        {
                            "name": "s1",
                            "type": "agent",
                            "agent": "diag",
                            "prompt": "test",
                            "output_key": "r1",
                            "spawn": "pre-deployed",
                        }
                    ]
                },
            },
        )
        assert response.status_code == 403

    def test_authz_context_captured_at_trigger(
        self,
        mock_client: Any,
    ) -> None:
        """WorkflowInput includes authz_context with owner identity."""
        app = FastAPI()
        router = build_temporal_router(mock_client)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 202

        call_args = mock_client.start_workflow.call_args
        wf_input = call_args[0][1]  # second positional arg
        assert wf_input.authz_context is not None
        assert wf_input.authz_context.owner_username == "anonymous"
        assert wf_input.authz_context.workflow_name == "test-wf"

    def test_approver_identity_in_audit(
        self,
        mocker: MockerFixture,
        mock_client: Any,
    ) -> None:
        """Approval emits audit event with approver identity."""
        mock_emit = mocker.patch("cloud_agents.workflow.temporal_api.emit_audit")

        app = FastAPI()
        router = build_temporal_router(mock_client)
        app.include_router(router)
        test_client = TestClient(app)

        test_client.post(
            "/v1/workflows/wf-test-1/approve",
            json={"step_name": "approve-step", "decision": "approved"},
        )

        approved_calls = [
            c
            for c in mock_emit.call_args_list
            if c[1].get("event_type") == "step_approved"
        ]
        assert len(approved_calls) == 1
        details = approved_calls[0][1]["details"]
        assert "approver" in details
        approver = details["approver"]
        assert approver["username"] == "anonymous"
        assert "approved_at" in approver

    def test_approve_signal_includes_approver_info(
        self,
        mocker: MockerFixture,
        mock_client: Any,
    ) -> None:
        """Approval signal passes approver username and uid to workflow."""
        mocker.patch("cloud_agents.workflow.temporal_api.emit_audit")

        app = FastAPI()
        router = build_temporal_router(mock_client)
        app.include_router(router)
        test_client = TestClient(app)

        test_client.post(
            "/v1/workflows/wf-test-1/approve",
            json={"step_name": "approve-step", "decision": "approved"},
        )

        handle = mock_client.get_workflow_handle.return_value
        handle.signal.assert_called_once()
        signal_args = handle.signal.call_args
        args_list = signal_args.kwargs.get("args") or signal_args[1].get("args", [])
        # args: [step_name, decision, selected_option_id, username, uid]
        assert len(args_list) == 5
        assert args_list[0] == "approve-step"
        assert args_list[1] == "approved"
        assert args_list[2] is None  # selected_option_id
        assert args_list[3] == "anonymous"  # approver_username
        assert args_list[4] is None  # approver_uid (anonymous has no uid)


class TestSSEEventStream:
    """Tests for GET /v1/workflows/{id}/events SSE endpoint."""

    def _make_status(self, steps, events):
        """Build a mock WorkflowStatus."""
        from cloud_agents.workflow.temporal_models import StepResult, WorkflowEvent, WorkflowStatus

        step_results = {
            k: StepResult(**v) if isinstance(v, dict) else v
            for k, v in steps.items()
        }
        event_objs = [
            WorkflowEvent(**e) if isinstance(e, dict) else e
            for e in events
        ]
        return WorkflowStatus(steps=step_results, events=event_objs)

    def _make_describe(self, status_name="RUNNING"):
        """Build a mock workflow description with execution status."""
        from unittest.mock import MagicMock

        from temporalio.client import WorkflowExecutionStatus

        desc = MagicMock()
        desc.status = getattr(WorkflowExecutionStatus, status_name)
        return desc

    def _collect_sse(self, response) -> list[dict]:
        """Parse SSE data lines from a streaming response."""
        import ast
        import json

        events = []
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                raw = line[6:]
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    events.append(ast.literal_eval(raw))
        return events

    def test_paused_workflow_does_not_emit_completed(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE should NOT emit workflow.completed when workflow is paused."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        call_count = 0

        async def query_status(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise Exception("stop polling")
            return self._make_status(
                steps={"diagnosis": {"status": "completed"}},
                events=[
                    {"type": "step.started", "step": "diagnose", "timestamp": "t1"},
                    {"type": "step.completed", "step": "diagnose", "timestamp": "t2"},
                    {"type": "workflow.paused", "step": "approve-fix", "timestamp": "t3"},
                ],
            )

        handle.query = query_status
        handle.describe = mocker.AsyncMock(return_value=self._make_describe("RUNNING"))

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-paused/events")
        events = self._collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "workflow.paused" in event_types
        assert "workflow.completed" not in event_types

    def test_all_terminal_emits_completed(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE emits workflow.completed when Temporal reports workflow finished."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=self._make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(return_value=self._make_status(
            steps={
                "diagnosis": {"status": "completed"},
                "approval": {"status": "completed", "output": {"approved": True}},
                "fix": {"status": "completed"},
            },
            events=[
                {"type": "step.started", "step": "diagnose", "timestamp": "t1"},
                {"type": "step.completed", "step": "diagnose", "timestamp": "t2"},
                {"type": "step.completed", "step": "approve", "timestamp": "t3"},
                {"type": "step.started", "step": "fix", "timestamp": "t4"},
                {"type": "step.completed", "step": "fix", "timestamp": "t5"},
            ],
        ))

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-done/events")
        events = self._collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "workflow.completed" in event_types
        assert event_types[-1] == "workflow.completed"

    def test_paused_then_resolved_emits_completed(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE emits workflow.completed after paused step is resolved."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        call_count = 0

        async def query_status(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._make_status(
                    steps={"diagnosis": {"status": "completed"}},
                    events=[
                        {"type": "step.completed", "step": "diagnose", "timestamp": "t1"},
                        {"type": "workflow.paused", "step": "approve", "timestamp": "t2"},
                    ],
                )
            else:
                return self._make_status(
                    steps={
                        "diagnosis": {"status": "completed"},
                        "approval": {"status": "completed", "output": {"approved": True}},
                        "fix": {"status": "completed"},
                    },
                    events=[
                        {"type": "step.completed", "step": "diagnose", "timestamp": "t1"},
                        {"type": "workflow.paused", "step": "approve", "timestamp": "t2"},
                        {"type": "step.completed", "step": "approve", "timestamp": "t3"},
                        {"type": "step.completed", "step": "fix", "timestamp": "t4"},
                    ],
                )

        handle.query = query_status

        desc_count = 0

        async def describe_status():
            nonlocal desc_count
            desc_count += 1
            if desc_count == 1:
                return self._make_describe("RUNNING")
            return self._make_describe("COMPLETED")

        handle.describe = describe_status

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-resume/events")
        events = self._collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "workflow.paused" in event_types
        assert "workflow.completed" in event_types
        assert event_types[-1] == "workflow.completed"

    def test_events_streamed_incrementally(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE streams events incrementally using cursor-based dedup."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        call_count = 0

        async def query_status(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._make_status(
                    steps={},
                    events=[
                        {"type": "step.started", "step": "s1", "timestamp": "t1"},
                    ],
                )
            else:
                return self._make_status(
                    steps={"r1": {"status": "completed"}},
                    events=[
                        {"type": "step.started", "step": "s1", "timestamp": "t1"},
                        {"type": "step.completed", "step": "s1", "timestamp": "t2"},
                    ],
                )

        handle.query = query_status

        desc_count = 0

        async def describe_status():
            nonlocal desc_count
            desc_count += 1
            if desc_count == 1:
                return self._make_describe("RUNNING")
            return self._make_describe("COMPLETED")

        handle.describe = describe_status

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-incr/events")
        events = self._collect_sse(response)

        started_count = sum(1 for e in events if e.get("type") == "step.started")
        completed_count = sum(1 for e in events if e.get("type") == "step.completed")
        assert started_count == 1, "step.started should appear exactly once (no duplicates)"
        assert completed_count == 1

    def test_failed_step_is_terminal(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE emits workflow.completed when Temporal reports workflow finished after failure."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=self._make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(return_value=self._make_status(
            steps={
                "diagnosis": {"status": "failed", "error": "retries exhausted"},
                "escalation": {"status": "escalated"},
            },
            events=[
                {"type": "step.started", "step": "diagnose", "timestamp": "t1"},
                {"type": "step.failed", "step": "diagnose", "timestamp": "t2"},
            ],
        ))

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-fail/events")
        events = self._collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "workflow.completed" in event_types

    def test_sse_data_is_valid_json(
        self,
        mocker: MockerFixture,
    ) -> None:
        """SSE data lines must be valid JSON, not Python dict repr."""
        import json as json_mod

        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=self._make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(return_value=self._make_status(
            steps={"r1": {"status": "completed"}},
            events=[
                {"type": "step.started", "step": "s1", "timestamp": "t1"},
                {"type": "step.completed", "step": "s1", "timestamp": "t2"},
            ],
        ))

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-json/events")
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                raw = line[6:]
                json_mod.loads(raw)  # must not raise JSONDecodeError

    def test_denied_step_with_paused_not_terminal(
        self,
        mocker: MockerFixture,
    ) -> None:
        """workflow.paused then step.denied — Temporal reports completed."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=self._make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(return_value=self._make_status(
            steps={
                "diagnosis": {"status": "completed"},
                "approval": {"status": "denied"},
            },
            events=[
                {"type": "step.completed", "step": "diagnose", "timestamp": "t1"},
                {"type": "workflow.paused", "step": "approve", "timestamp": "t2"},
                {"type": "step.denied", "step": "approve", "timestamp": "t3"},
            ],
        ))

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-denied/events")
        events = self._collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "workflow.completed" in event_types


class TestBareRaiseGuard:
    """Tests that non-RPC exceptions from start_workflow return generic 500."""

    def test_non_rpc_error_returns_500(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Non-RPC exception from start_workflow returns generic 500."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = AsyncMock(
            side_effect=RuntimeError("Temporal connection lost: secret=sk-abc123")
        )

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Internal workflow error"
        assert "sk-abc123" not in str(body)
        assert "Temporal connection lost" not in str(body)

    def test_rpc_already_exists_still_returns_409(
        self,
        mocker: MockerFixture,
    ) -> None:
        """RPCError ALREADY_EXISTS still returns 409, not generic 500."""
        from temporalio.service import RPCError, RPCStatusCode

        exc = RPCError(
            message="Workflow execution already started",
            status=RPCStatusCode.ALREADY_EXISTS,
            raw_grpc_status=None,
        )
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = AsyncMock(side_effect=exc)

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 409

    def test_generic_exception_returns_500(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Generic Exception from start_workflow returns 500 with safe message."""
        mock_temporal = mocker.MagicMock()
        mock_temporal.start_workflow = AsyncMock(
            side_effect=Exception("unexpected internal error with password=hunter2")
        )

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/run", json=VALID_RUN_PAYLOAD)
        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Internal workflow error"
        assert "hunter2" not in str(body)


class TestAuthzContextLoadedForLaterOperations:
    """Tests that later operations load persisted authz context."""

    def test_approve_loads_authz_context(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Approve endpoint queries authz context before authorization."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            CallerIdentity,
            WorkflowAction,
            WorkflowAuthorizer,
            WorkflowResource,
        )

        class OwnerOnlyAuthorizer(WorkflowAuthorizer):
            """Authorizer that only allows the workflow owner."""

            async def authorize(
                self,
                identity: CallerIdentity,
                action: WorkflowAction,
                resource: WorkflowResource,
            ) -> AuthzDecision:
                """Allow only if caller matches owner."""
                if resource.owner and identity.username != resource.owner:
                    return AuthzDecision(
                        allowed=False,
                        reason=f"only owner '{resource.owner}' can {action.value}",
                    )
                return AuthzDecision(allowed=True)

        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        # Workflow was triggered by "sre-bot", anonymous is not the owner
        handle.query.return_value = {
            "owner_username": "sre-bot",
            "workflow_name": "diag",
            "namespace": "prod",
            "owner_groups": [],
        }
        mock_temporal.get_workflow_handle.return_value = handle

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal, authorizer=OwnerOnlyAuthorizer()
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        # anonymous caller != sre-bot owner => should be denied
        response = test_client.post(
            "/v1/workflows/wf-owned/approve",
            json={"step_name": "s1", "decision": "approved"},
        )
        assert response.status_code == 403
        assert "sre-bot" in response.json()["detail"]

    def test_view_loads_authz_context(
        self,
        mocker: MockerFixture,
    ) -> None:
        """GET /{workflow_id} queries authz context before authorization."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            CallerIdentity,
            WorkflowAction,
            WorkflowAuthorizer,
            WorkflowResource,
        )

        class OwnerOnlyAuthorizer(WorkflowAuthorizer):
            """Authorizer that only allows the workflow owner."""

            async def authorize(
                self,
                identity: CallerIdentity,
                action: WorkflowAction,
                resource: WorkflowResource,
            ) -> AuthzDecision:
                """Allow only if caller matches owner."""
                if resource.owner and identity.username != resource.owner:
                    return AuthzDecision(
                        allowed=False,
                        reason=f"only owner '{resource.owner}' can {action.value}",
                    )
                return AuthzDecision(allowed=True)

        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        handle.query.return_value = {
            "owner_username": "sre-bot",
            "workflow_name": "diag",
            "namespace": "prod",
            "owner_groups": [],
        }
        mock_temporal.get_workflow_handle.return_value = handle

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal, authorizer=OwnerOnlyAuthorizer()
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.get("/v1/workflows/wf-owned")
        assert response.status_code == 403

    def test_cancel_loads_authz_context(
        self,
        mocker: MockerFixture,
    ) -> None:
        """POST /{workflow_id}/cancel queries authz context before authorization."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            CallerIdentity,
            WorkflowAction,
            WorkflowAuthorizer,
            WorkflowResource,
        )

        class OwnerOnlyAuthorizer(WorkflowAuthorizer):
            """Authorizer that only allows the workflow owner."""

            async def authorize(
                self,
                identity: CallerIdentity,
                action: WorkflowAction,
                resource: WorkflowResource,
            ) -> AuthzDecision:
                """Allow only if caller matches owner."""
                if resource.owner and identity.username != resource.owner:
                    return AuthzDecision(
                        allowed=False,
                        reason=f"only owner '{resource.owner}' can {action.value}",
                    )
                return AuthzDecision(allowed=True)

        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        handle.query.return_value = {
            "owner_username": "sre-bot",
            "workflow_name": "diag",
            "namespace": "prod",
            "owner_groups": [],
        }
        mock_temporal.get_workflow_handle.return_value = handle

        app = FastAPI()
        router = build_temporal_router(
            mock_temporal, authorizer=OwnerOnlyAuthorizer()
        )
        app.include_router(router)
        test_client = TestClient(app, raise_server_exceptions=False)

        response = test_client.post("/v1/workflows/wf-owned/cancel")
        assert response.status_code == 403


class TestGetWorkflowHandoff:
    """Tests for GET /v1/workflows/{id}/handoff (T15 Task 4)."""

    def _mock_workflow_context(self, mock_client: Any, mocker: MockerFixture) -> None:
        """Set up mock to return both status and workflow context."""
        handle = mock_client.get_workflow_handle.return_value

        status_result = mocker.MagicMock()
        status_result.model_dump = lambda: {
            "steps": {
                "r1": {
                    "status": "completed",
                    "output": {"summary": "found issues"},
                    "error": None,
                },
                "r2": {
                    "status": "failed",
                    "output": None,
                    "error": "retries exhausted",
                },
            },
            "events": [
                {"type": "step.started", "step": "diagnose", "timestamp": "2026-01-01T00:00:00"},
                {"type": "step.failed", "step": "fix-hosts", "timestamp": "2026-01-01T00:01:00"},
            ],
        }
        status_result.steps = {
            "r1": mocker.MagicMock(
                status="completed",
                output={"summary": "found issues"},
                error=None,
                model_dump=lambda: {"status": "completed", "output": {"summary": "found issues"}, "error": None},
            ),
            "r2": mocker.MagicMock(
                status="failed",
                output=None,
                error="retries exhausted",
                model_dump=lambda: {"status": "failed", "output": None, "error": "retries exhausted"},
            ),
        }
        status_result.events = [
            mocker.MagicMock(
                type="step.started",
                step="diagnose",
                timestamp="2026-01-01T00:00:00",
                model_dump=lambda: {"type": "step.started", "step": "diagnose", "timestamp": "2026-01-01T00:00:00"},
            ),
            mocker.MagicMock(
                type="step.failed",
                step="fix-hosts",
                timestamp="2026-01-01T00:01:00",
                model_dump=lambda: {"type": "step.failed", "step": "fix-hosts", "timestamp": "2026-01-01T00:01:00"},
            ),
        ]

        workflow_context = {
            "definition": {"metadata": {"name": "diagnose-fix"}, "spec": {"steps": []}},
            "input_prompt": "Fix the broken hosts",
            "provider_name": "openai",
            "provider_model": "gpt-4",
        }

        def side_effect(query_fn):
            from cloud_agents.workflow.temporal_workflow import AgentWorkflow
            if query_fn == AgentWorkflow.get_status:
                return status_result
            if query_fn == AgentWorkflow.get_workflow_context:
                return workflow_context
            if query_fn == AgentWorkflow.get_authz_context:
                return None
            if query_fn == AgentWorkflow.get_step_transcripts:
                return {}
            return None

        handle.query = mocker.AsyncMock(side_effect=side_effect)

    def test_handoff_returns_200(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """GET /handoff returns 200 with context."""
        self._mock_workflow_context(mock_client, mocker)
        response = client.get("/v1/workflows/wf-test-1/handoff")
        assert response.status_code == 200

    def test_handoff_contains_context_markdown(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Response includes context_markdown as primary interface."""
        self._mock_workflow_context(mock_client, mocker)
        response = client.get("/v1/workflows/wf-test-1/handoff")
        data = response.json()
        assert "context_markdown" in data
        assert "Investigation Handoff" in data["context_markdown"]

    def test_handoff_contains_launch_command(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Response includes a launch_command."""
        self._mock_workflow_context(mock_client, mocker)
        response = client.get("/v1/workflows/wf-test-1/handoff")
        data = response.json()
        assert "launch_command" in data
        assert "claude" in data["launch_command"]

    def test_handoff_contains_workflow_id(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """Response includes the workflow_id."""
        self._mock_workflow_context(mock_client, mocker)
        response = client.get("/v1/workflows/wf-test-1/handoff")
        data = response.json()
        assert data["workflow_id"] == "wf-test-1"

    def test_handoff_nonexistent_workflow_returns_404(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """GET /handoff for non-existent workflow returns 404."""
        handle = mock_client.get_workflow_handle.return_value
        handle.query = mocker.AsyncMock(
            side_effect=Exception("workflow not found")
        )

        response = client.get("/v1/workflows/wf-nonexistent/handoff")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestTranscriptEndpoint:
    """Tests for GET /v1/workflows/{id}/steps/{step}/transcript."""

    def _mock_transcripts(
        self,
        mock_client: Any,
        mocker: MockerFixture,
        transcripts: dict | None = None,
    ) -> None:
        """Set up mock for transcript query."""
        handle = mock_client.get_workflow_handle.return_value

        default_transcripts = transcripts or {
            "r1": {
                "step_name": "diagnose",
                "events": [
                    {"ts": "t1", "type": "tool_call", "data": {"name": "kubectl"}},
                    {"ts": "t2", "type": "result", "data": {"output": "ok"}},
                ],
                "cost_usd": 0.05,
                "input_tokens": 1000,
                "output_tokens": 500,
                "duration_ms": 1500,
            },
        }

        async def mock_query(query_fn):
            from cloud_agents.workflow.temporal_workflow import AgentWorkflow

            if query_fn == AgentWorkflow.get_step_transcripts:
                return default_transcripts
            if query_fn == AgentWorkflow.get_authz_context:
                return None
            return mocker.MagicMock(model_dump=lambda: {"steps": {}, "events": []})

        handle.query = mocker.AsyncMock(side_effect=mock_query)

    def test_transcript_returns_200(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """GET /steps/{step}/transcript returns 200 with transcript data."""
        self._mock_transcripts(mock_client, mocker)
        response = client.get("/v1/workflows/wf-test-1/steps/r1/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["step_name"] == "diagnose"
        assert len(data["events"]) == 2
        assert data["cost_usd"] == 0.05

    def test_transcript_not_found_returns_404(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """GET /steps/{step}/transcript for unknown step returns 404."""
        self._mock_transcripts(mock_client, mocker, transcripts={})
        response = client.get("/v1/workflows/wf-test-1/steps/nonexistent/transcript")
        assert response.status_code == 404

    def test_transcript_workflow_not_found_returns_404(
        self,
        client: TestClient,
        mock_client: Any,
        mocker: MockerFixture,
    ) -> None:
        """GET /steps/{step}/transcript for non-existent workflow returns 404."""
        handle = mock_client.get_workflow_handle.return_value
        handle.query = mocker.AsyncMock(
            side_effect=Exception("workflow not found")
        )
        response = client.get("/v1/workflows/wf-missing/steps/r1/transcript")
        assert response.status_code == 404


class TestTranscriptWithPostgres:
    """Tests for transcript endpoint with PostgreSQL store integration."""

    @pytest.fixture
    def mock_store(self, mocker: MockerFixture) -> Any:
        """Create a mock TranscriptStore."""
        return mocker.AsyncMock()

    @pytest.fixture
    def app_with_store(self, mock_client: Any, mock_store: Any) -> FastAPI:
        """Create a test FastAPI app with transcript store."""
        app = FastAPI()
        router = build_temporal_router(mock_client, transcript_store=mock_store)
        app.include_router(router)
        return app

    @pytest.fixture
    def client_with_store(self, app_with_store: FastAPI) -> TestClient:
        """Create a test client with transcript store."""
        return TestClient(app_with_store)

    def _mock_workflow_query(self, mock_client: Any, mocker: MockerFixture) -> None:
        """Set up workflow query to return truncated transcripts."""
        handle = mock_client.get_workflow_handle.return_value

        async def mock_query(query_fn):
            from cloud_agents.workflow.temporal_workflow import AgentWorkflow
            if query_fn == AgentWorkflow.get_step_transcripts:
                return {
                    "r1": {
                        "step_name": "diagnose",
                        "events": [
                            {"ts": "t1", "type": "tool_call", "data": {"name": "kubectl"}},
                        ],
                        "cost_usd": 0.01,
                        "input_tokens": 50,
                        "output_tokens": 25,
                        "duration_ms": 500,
                    },
                }
            if query_fn == AgentWorkflow.get_authz_context:
                return None
            return mocker.MagicMock(model_dump=lambda: {"steps": {}, "events": []})

        handle.query = mocker.AsyncMock(side_effect=mock_query)

    def test_reads_from_postgres_first(
        self, client_with_store: TestClient, mock_client: Any,
        mock_store: Any, mocker: MockerFixture,
    ) -> None:
        """Transcript endpoint reads from Postgres when available."""
        from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent
        full_transcript = StepTranscript(
            step_name="diagnose",
            events=[
                TranscriptEvent(ts="t1", type="tool_call", data={"name": "kubectl"}),
                TranscriptEvent(ts="t2", type="result", data={"output": "full data"}),
            ],
            cost_usd=0.05, input_tokens=1000, output_tokens=500, duration_ms=5000,
        )
        mock_store.get = mocker.AsyncMock(return_value=full_transcript)
        self._mock_workflow_query(mock_client, mocker)
        response = client_with_store.get("/v1/workflows/wf-test-1/steps/r1/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["truncated"] is False
        assert len(data["events"]) == 2

    def test_falls_back_to_workflow_query_when_postgres_empty(
        self, client_with_store: TestClient, mock_client: Any,
        mock_store: Any, mocker: MockerFixture,
    ) -> None:
        """Falls back to workflow query state when Postgres has no data."""
        mock_store.get = mocker.AsyncMock(return_value=None)
        self._mock_workflow_query(mock_client, mocker)
        response = client_with_store.get("/v1/workflows/wf-test-1/steps/r1/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["truncated"] is True

    def test_falls_back_to_workflow_query_when_postgres_fails(
        self, client_with_store: TestClient, mock_client: Any,
        mock_store: Any, mocker: MockerFixture,
    ) -> None:
        """Falls back to workflow query state when Postgres is unreachable."""
        mock_store.get = mocker.AsyncMock(side_effect=RuntimeError("connection refused"))
        self._mock_workflow_query(mock_client, mocker)
        response = client_with_store.get("/v1/workflows/wf-test-1/steps/r1/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["truncated"] is True

    def test_no_store_marks_truncated(
        self, client: TestClient, mock_client: Any, mocker: MockerFixture,
    ) -> None:
        """Without transcript store, response has truncated=True."""
        handle = mock_client.get_workflow_handle.return_value

        async def mock_query(query_fn):
            from cloud_agents.workflow.temporal_workflow import AgentWorkflow
            if query_fn == AgentWorkflow.get_step_transcripts:
                return {"r1": {"step_name": "diagnose", "events": []}}
            if query_fn == AgentWorkflow.get_authz_context:
                return None
            return mocker.MagicMock(model_dump=lambda: {"steps": {}, "events": []})

        handle.query = mocker.AsyncMock(side_effect=mock_query)
        response = client.get("/v1/workflows/wf-test-1/steps/r1/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["truncated"] is True
