"""Unit tests for workflow authorization models and framework."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from cloud_agents.workflow.authorization import (
    ApproverInfo,
    AuthzDecision,
    CallerIdentity,
    NoopAuthorizer,
    WorkflowAction,
    WorkflowAuthzContext,
    WorkflowResource,
    get_caller_identity,
    parse_namespace_from_sa_username,
)


class TestCallerIdentity:
    """Tests for CallerIdentity model and get_caller_identity dependency."""

    async def test_anonymous_fallback_shared_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No request.state.caller_identity, WORKFLOW_AUTHZ=none -> anonymous."""
        monkeypatch.delenv("WORKFLOW_AUTHZ", raising=False)
        request = MagicMock()
        del request.state.caller_identity  # force AttributeError on access

        identity = await get_caller_identity(request)

        assert identity.username == "anonymous"
        assert identity.auth_mode == "shared_secret"

    async def test_fail_closed_when_authz_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No identity, WORKFLOW_AUTHZ=policy -> 401."""
        monkeypatch.setenv("WORKFLOW_AUTHZ", "policy")
        request = MagicMock()
        del request.state.caller_identity

        with pytest.raises(HTTPException) as exc_info:
            await get_caller_identity(request)

        assert exc_info.value.status_code == 401

    async def test_identity_from_request_state(self) -> None:
        """request.state.caller_identity set -> returned as-is."""
        expected = CallerIdentity(
            username="system:serviceaccount:prod:sre-bot",
            uid="abc-123",
            groups=["system:serviceaccounts"],
            auth_mode="sa_token",
        )
        request = MagicMock()
        request.state.caller_identity = expected

        identity = await get_caller_identity(request)

        assert identity is expected
        assert identity.username == "system:serviceaccount:prod:sre-bot"
        assert identity.uid == "abc-123"
        assert identity.groups == ["system:serviceaccounts"]
        assert identity.auth_mode == "sa_token"

    def test_defaults(self) -> None:
        """Test default field values for CallerIdentity."""
        identity = CallerIdentity(username="admin", auth_mode="jwt")
        assert identity.uid is None
        assert identity.groups == []

    async def test_anonymous_fallback_explicit_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WORKFLOW_AUTHZ explicitly set to 'none' -> anonymous fallback."""
        monkeypatch.setenv("WORKFLOW_AUTHZ", "none")
        request = MagicMock()
        del request.state.caller_identity

        identity = await get_caller_identity(request)

        assert identity.username == "anonymous"
        assert identity.auth_mode == "shared_secret"


class TestNoopAuthorizer:
    """Tests for NoopAuthorizer."""

    async def test_allows_everything(self) -> None:
        """NoopAuthorizer always returns allowed=True."""
        authz = NoopAuthorizer()
        decision = await authz.authorize(
            CallerIdentity(username="anyone", auth_mode="shared_secret"),
            WorkflowAction.TRIGGER,
            WorkflowResource(),
        )
        assert decision.allowed is True
        assert decision.reason == "authorization disabled"

    async def test_allows_all_actions(self) -> None:
        """NoopAuthorizer allows every action type."""
        authz = NoopAuthorizer()
        identity = CallerIdentity(username="test", auth_mode="jwt")
        for action in WorkflowAction:
            decision = await authz.authorize(
                identity,
                action,
                WorkflowResource(workflow_id="w-1", workflow_name="diag"),
            )
            assert decision.allowed is True


class TestWorkflowAction:
    """Tests for WorkflowAction enum."""

    def test_all_actions_defined(self) -> None:
        """Verify all expected actions exist."""
        expected = {"trigger", "approve", "view", "cancel", "view_defs", "manage_defs"}
        actual = {a.value for a in WorkflowAction}
        assert actual == expected

    def test_string_enum(self) -> None:
        """WorkflowAction values are strings."""
        assert WorkflowAction.TRIGGER == "trigger"
        assert isinstance(WorkflowAction.APPROVE, str)


class TestWorkflowResource:
    """Tests for WorkflowResource model."""

    def test_defaults(self) -> None:
        """All fields default to None."""
        resource = WorkflowResource()
        assert resource.workflow_id is None
        assert resource.workflow_name is None
        assert resource.owner is None
        assert resource.namespace is None
        assert resource.step is None

    def test_full_resource(self) -> None:
        """All fields populated."""
        resource = WorkflowResource(
            workflow_id="w-1",
            workflow_name="diag",
            owner="sre-bot",
            namespace="prod",
            step="check-health",
        )
        assert resource.workflow_id == "w-1"
        assert resource.workflow_name == "diag"
        assert resource.owner == "sre-bot"
        assert resource.namespace == "prod"
        assert resource.step == "check-health"


class TestAuthzDecision:
    """Tests for AuthzDecision model."""

    def test_allowed(self) -> None:
        """Test allowed decision."""
        decision = AuthzDecision(allowed=True, reason="rule matched")
        assert decision.allowed is True
        assert decision.reason == "rule matched"

    def test_denied(self) -> None:
        """Test denied decision."""
        decision = AuthzDecision(allowed=False, reason="no matching rule")
        assert decision.allowed is False
        assert decision.reason == "no matching rule"

    def test_default_reason(self) -> None:
        """Reason defaults to empty string."""
        decision = AuthzDecision(allowed=True)
        assert decision.reason == ""


class TestWorkflowAuthzContext:
    """Tests for WorkflowAuthzContext model."""

    def test_captures_owner(self) -> None:
        """Context captures workflow owner identity."""
        ctx = WorkflowAuthzContext(owner_username="sre-bot", workflow_name="diag")
        assert ctx.owner_username == "sre-bot"
        assert ctx.workflow_name == "diag"

    def test_defaults(self) -> None:
        """Optional fields default correctly."""
        ctx = WorkflowAuthzContext(owner_username="admin", workflow_name="scan")
        assert ctx.owner_groups == []
        assert ctx.namespace is None

    def test_full_context(self) -> None:
        """All fields populated."""
        ctx = WorkflowAuthzContext(
            owner_username="system:serviceaccount:prod:sre-bot",
            owner_groups=["system:serviceaccounts", "team:sre"],
            workflow_name="diag",
            namespace="prod",
        )
        assert ctx.owner_username == "system:serviceaccount:prod:sre-bot"
        assert ctx.owner_groups == ["system:serviceaccounts", "team:sre"]
        assert ctx.namespace == "prod"


class TestApproverInfo:
    """Tests for ApproverInfo model."""

    def test_records_identity(self) -> None:
        """ApproverInfo captures approver username and timestamp."""
        info = ApproverInfo(username="admin", approved_at="2026-01-01T00:00:00Z")
        assert info.username == "admin"
        assert info.approved_at == "2026-01-01T00:00:00Z"

    def test_uid_default(self) -> None:
        """UID defaults to None."""
        info = ApproverInfo(username="admin", approved_at="2026-01-01T00:00:00Z")
        assert info.uid is None

    def test_uid_populated(self) -> None:
        """UID can be explicitly set."""
        info = ApproverInfo(
            username="admin", uid="uid-456", approved_at="2026-01-01T00:00:00Z"
        )
        assert info.uid == "uid-456"


class TestNamespaceParsing:
    """Tests for parse_namespace_from_sa_username."""

    def test_sa_username_format(self) -> None:
        """Parses namespace from K8s SA username format."""
        assert (
            parse_namespace_from_sa_username("system:serviceaccount:prod:sre-bot")
            == "prod"
        )

    def test_non_sa_returns_none(self) -> None:
        """Non-SA username returns None."""
        assert parse_namespace_from_sa_username("admin") is None

    def test_partial_format_returns_none(self) -> None:
        """Partial SA format returns None."""
        assert parse_namespace_from_sa_username("system:serviceaccount") is None

    def test_wrong_prefix_returns_none(self) -> None:
        """Correct number of parts but wrong prefix returns None."""
        assert parse_namespace_from_sa_username("not:serviceaccount:ns:name") is None

    def test_wrong_second_segment_returns_none(self) -> None:
        """Correct first segment but wrong second returns None."""
        assert parse_namespace_from_sa_username("system:notsa:ns:name") is None

    def test_extra_colons_returns_none(self) -> None:
        """Too many segments returns None."""
        assert (
            parse_namespace_from_sa_username("system:serviceaccount:ns:name:extra")
            is None
        )


class TestBearerMiddlewareSetsCallerIdentity:
    """Tests that BearerAuthMiddleware sets caller_identity on request state."""

    def test_bearer_middleware_sets_caller_identity(self) -> None:
        """BearerAuthMiddleware sets anonymous CallerIdentity on request state."""
        from cloud_agents.runtime.auth import BearerAuthMiddleware

        import fastapi
        from starlette.testclient import TestClient

        token = "test-secret-token"

        app = fastapi.FastAPI()

        # Use Depends(get_caller_identity) to read what the middleware set,
        # avoiding the from __future__ import annotations + Request issue.
        @app.get("/check-identity")
        async def check_identity(
            caller=fastapi.Depends(get_caller_identity),
        ):
            return {
                "username": caller.username,
                "auth_mode": caller.auth_mode,
            }

        app.add_middleware(BearerAuthMiddleware, tokens=[token])

        client = TestClient(app)
        response = client.get(
            "/check-identity", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "anonymous"
        assert data["auth_mode"] == "shared_secret"

    def test_token_review_middleware_sets_caller_identity(self) -> None:
        """TokenReviewAuthMiddleware sets CallerIdentity from TokenReview response."""
        from cloud_agents.runtime.auth import TokenReviewAuthMiddleware

        import fastapi
        from starlette.testclient import TestClient

        # Build a mock TokenReview result
        mock_result = MagicMock()
        mock_result.status.authenticated = True
        mock_result.status.user.username = "system:serviceaccount:prod:sre-bot"
        mock_result.status.user.uid = "uid-abc-123"
        mock_result.status.user.groups = ["system:serviceaccounts", "team:sre"]

        app = fastapi.FastAPI()

        @app.get("/check-identity")
        async def check_identity(
            caller=fastapi.Depends(get_caller_identity),
        ):
            return {
                "username": caller.username,
                "uid": caller.uid,
                "groups": caller.groups,
                "auth_mode": caller.auth_mode,
            }

        app.add_middleware(TokenReviewAuthMiddleware)

        client = TestClient(app)

        with patch.object(
            TokenReviewAuthMiddleware,
            "_validate_token",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.get(
                "/check-identity",
                headers={"Authorization": "Bearer fake-sa-token"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "system:serviceaccount:prod:sre-bot"
        assert data["uid"] == "uid-abc-123"
        assert data["groups"] == ["system:serviceaccounts", "team:sre"]
        assert data["auth_mode"] == "sa_token"


class TestOwnerScopedAuthzDeniesNonOwner:
    """Tests that owner-scoped authorization denies non-owners on existing workflows."""

    async def test_owner_scoped_policy_denies_non_owner(self) -> None:
        """An owner-scoped policy rule denies a non-owner on an existing workflow."""
        from cloud_agents.workflow.authorization import (
            AuthzDecision,
            WorkflowAuthorizer,
        )

        class OwnerOnlyAuthorizer(WorkflowAuthorizer):
            """Authorizer that only allows the workflow owner."""

            async def authorize(
                self,
                identity: CallerIdentity,
                action: WorkflowAction,
                resource: WorkflowResource,
            ) -> AuthzDecision:
                """Allow only if caller is the workflow owner."""
                if resource.owner and identity.username != resource.owner:
                    return AuthzDecision(
                        allowed=False,
                        reason=f"only owner '{resource.owner}' can {action.value}",
                    )
                return AuthzDecision(allowed=True)

        authorizer = OwnerOnlyAuthorizer()

        # Non-owner tries to view a workflow owned by someone else
        caller = CallerIdentity(username="intruder", auth_mode="sa_token")
        resource = WorkflowResource(
            workflow_id="wf-1",
            owner="sre-bot",
            namespace="prod",
        )

        decision = await authorizer.authorize(caller, WorkflowAction.VIEW, resource)
        assert decision.allowed is False
        assert "sre-bot" in decision.reason

        # Owner can view
        owner = CallerIdentity(username="sre-bot", auth_mode="sa_token")
        decision = await authorizer.authorize(owner, WorkflowAction.VIEW, resource)
        assert decision.allowed is True
