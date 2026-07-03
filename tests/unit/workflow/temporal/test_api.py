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
        """SSE emits workflow.completed when all steps are terminal."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

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
        """SSE emits workflow.completed when step fails (no pause)."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

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
        """workflow.paused then step.denied resolves the pause — stream can complete."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

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
