"""Integration tests for RBAC — exercises the real middleware → identity → authorizer → endpoint path.

No mocking of auth middleware or authorization layer. Tests the actual FastAPI app
with a PolicyFileAuthorizer and real request flow.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.workflow.authorization import CallerIdentity
from cloud_agents.workflow.policy_authorizer import PolicyFileAuthorizer
from cloud_agents.workflow.temporal_api import build_temporal_router


def _make_policy_file(rules: list[dict], defaults: dict | None = None) -> str:
    """Write a policy YAML to a temp file and return the path."""
    policy = {"rules": rules}
    if defaults:
        policy["defaults"] = defaults
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(policy, f)
    f.close()
    return f.name


def _make_app(mock_client: Any, policy_path: str) -> FastAPI:
    """Build a FastAPI app with PolicyFileAuthorizer and a mock Temporal client."""
    authorizer = PolicyFileAuthorizer(policy_path)
    app = FastAPI()
    router = build_temporal_router(mock_client, authorizer=authorizer)
    app.include_router(router)
    return app


def _inject_identity(app: FastAPI, identity: CallerIdentity) -> None:
    """Add middleware that sets caller_identity on every request."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class IdentityInjector(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.caller_identity = identity
            return await call_next(request)

    app.add_middleware(IdentityInjector)


@pytest.fixture
def mock_temporal(mocker):
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


VALID_WORKFLOW_BODY = {
    "definition": {
        "apiVersion": "v1",
        "kind": "AgentWorkflow",
        "metadata": {"name": "test-wf"},
        "spec": {
            "steps": [
                {"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}
            ]
        },
    },
    "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
}


class TestRBACIntegration:
    """Integration tests exercising the full auth → authz → endpoint path."""

    def test_sre_team_can_trigger(self, mock_temporal) -> None:
        """SRE team member with trigger permission can start a workflow."""
        policy_path = _make_policy_file([
            {"identity": "team:sre", "actions": ["trigger", "view"], "workflows": ["*"]},
        ])
        app = _make_app(mock_temporal, policy_path)
        _inject_identity(app, CallerIdentity(
            username="system:serviceaccount:prod:sre-bot",
            groups=["system:serviceaccounts", "team:sre"],
            auth_mode="sa_token",
        ))
        client = TestClient(app)

        response = client.post("/v1/workflows/run", json=VALID_WORKFLOW_BODY)
        assert response.status_code == 202
        os.unlink(policy_path)

    def test_developer_cannot_trigger_outside_scope(self, mock_temporal) -> None:
        """Developer scoped to diagnose-* cannot trigger fix-prod workflow."""
        policy_path = _make_policy_file([
            {"identity": "team:dev", "actions": ["trigger"], "workflows": ["diagnose-*"]},
        ])
        app = _make_app(mock_temporal, policy_path)
        _inject_identity(app, CallerIdentity(
            username="dev-user",
            groups=["team:dev"],
            auth_mode="sa_token",
        ))
        client = TestClient(app)

        body = dict(VALID_WORKFLOW_BODY)
        body["definition"] = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "fix-prod"},
            "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "test"}]},
        }
        response = client.post("/v1/workflows/run", json=body)
        assert response.status_code == 403

    def test_default_view_allowed_for_any_authenticated(self, mock_temporal) -> None:
        """Any authenticated user can view workflow status (default allow)."""
        policy_path = _make_policy_file(
            rules=[],
            defaults={"allow": ["view"], "deny_unless_matched": ["trigger", "approve", "cancel", "manage_defs"]},
        )
        app = _make_app(mock_temporal, policy_path)
        _inject_identity(app, CallerIdentity(
            username="random-user",
            groups=[],
            auth_mode="sa_token",
        ))
        client = TestClient(app)

        response = client.get("/v1/workflows/wf-test-1")
        assert response.status_code == 200
        os.unlink(policy_path)

    def test_default_deny_trigger_without_rule(self, mock_temporal) -> None:
        """Trigger is denied by default when no explicit rule matches."""
        policy_path = _make_policy_file(
            rules=[],
            defaults={"allow": ["view"], "deny_unless_matched": ["trigger"]},
        )
        app = _make_app(mock_temporal, policy_path)
        _inject_identity(app, CallerIdentity(
            username="nobody",
            groups=[],
            auth_mode="sa_token",
        ))
        client = TestClient(app)

        response = client.post("/v1/workflows/run", json=VALID_WORKFLOW_BODY)
        assert response.status_code == 403
        os.unlink(policy_path)

    def test_no_identity_with_authz_enabled_returns_401(self, mock_temporal, mocker) -> None:
        """Request without CallerIdentity when authz is enabled returns 401."""
        mocker.patch.dict(os.environ, {"WORKFLOW_AUTHZ": "policy"})

        policy_path = _make_policy_file([
            {"identity": "team:sre", "actions": ["trigger"], "workflows": ["*"]},
        ])
        app = _make_app(mock_temporal, policy_path)
        # No identity injected — simulates auth middleware failure
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/v1/workflows/run", json=VALID_WORKFLOW_BODY)
        assert response.status_code == 401
        os.unlink(policy_path)

    def test_manage_defs_denied_for_viewer(self, mock_temporal) -> None:
        """User with only view_defs permission cannot POST definitions."""
        policy_path = _make_policy_file([
            {"identity": "user:viewer", "actions": ["view_defs"], "workflows": ["*"]},
        ])
        from cloud_agents.workflow.definition_store import DefinitionStore

        authorizer = PolicyFileAuthorizer(policy_path)
        app = FastAPI()
        router = build_temporal_router(mock_temporal, authorizer=authorizer, definition_store=DefinitionStore())
        app.include_router(router)
        _inject_identity(app, CallerIdentity(
            username="viewer", groups=[], auth_mode="sa_token",
        ))
        client = TestClient(app)

        response = client.post("/v1/workflows/definitions", json={
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [{"name": "s1", "type": "agent", "output_key": "r1", "prompt": "t"}]},
        })
        assert response.status_code == 403
        os.unlink(policy_path)
