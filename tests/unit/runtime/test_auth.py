"""Unit tests for bearer auth multi-token support, rejection logging, and expiry.

Tests for T42: Token rotation and expiry for bearer auth (#25).
TDD — these tests are written BEFORE the implementation.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cloud_agents.runtime.auth import (
    EXEMPT_PATHS,
    BearerAuthMiddleware,
    get_api_token,
    get_api_tokens,
)
from cloud_agents.workflow.audit import AuditEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_with_bearer_middleware(tokens: list[str] | None = None) -> FastAPI:
    """Build a minimal FastAPI app with BearerAuthMiddleware.

    Parameters:
        tokens: List of valid tokens (may include :timestamp suffix).

    Returns:
        FastAPI app with middleware and a test endpoint.
    """
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    app.add_middleware(BearerAuthMiddleware, tokens=tokens)
    return app


def _app_with_dependency(tokens: list[str]) -> FastAPI:
    """Build a FastAPI app using create_bearer_auth_dependency.

    Parameters:
        tokens: List of valid tokens.

    Returns:
        FastAPI app with dependency-based auth on /test.
    """
    from fastapi import APIRouter, Depends

    from cloud_agents.runtime.auth import create_bearer_auth_dependency

    app = FastAPI()
    dep = create_bearer_auth_dependency(tokens)
    router = APIRouter(dependencies=[Depends(dep)])

    @router.get("/test")
    async def test_endpoint(request: Request):
        identity = getattr(request.state, "caller_identity", None)
        if identity:
            return {"ok": True, "username": identity.username}
        return {"ok": True}

    app.include_router(router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


# ===========================================================================
# Task 1: Multi-token support — get_api_tokens()
# ===========================================================================


class TestGetApiTokens:
    """Tests for get_api_tokens() multi-token parsing."""

    def test_comma_separated_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENT_API_TOKENS with comma-separated values returns list."""
        monkeypatch.setenv("AGENT_API_TOKENS", "tok1,tok2,tok3")
        monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
        tokens = get_api_tokens()
        assert tokens == ["tok1", "tok2", "tok3"]

    def test_single_token_backward_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENT_API_TOKEN (singular) still works for backward compat."""
        monkeypatch.delenv("AGENT_API_TOKENS", raising=False)
        monkeypatch.setenv("AGENT_API_TOKEN", "legacy-token")
        tokens = get_api_tokens()
        assert tokens == ["legacy-token"]

    def test_both_env_vars_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both AGENT_API_TOKENS and AGENT_API_TOKEN merge without duplicates."""
        monkeypatch.setenv("AGENT_API_TOKENS", "tok1,tok2")
        monkeypatch.setenv("AGENT_API_TOKEN", "tok3")
        tokens = get_api_tokens()
        assert "tok1" in tokens
        assert "tok2" in tokens
        assert "tok3" in tokens

    def test_both_env_vars_no_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENT_API_TOKEN already present in AGENT_API_TOKENS is not duplicated."""
        monkeypatch.setenv("AGENT_API_TOKENS", "tok1,tok2")
        monkeypatch.setenv("AGENT_API_TOKEN", "tok1")
        tokens = get_api_tokens()
        assert tokens.count("tok1") == 1

    def test_neither_env_var_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env vars set returns empty list."""
        monkeypatch.delenv("AGENT_API_TOKENS", raising=False)
        monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
        tokens = get_api_tokens()
        assert tokens == []

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace around tokens is stripped."""
        monkeypatch.setenv("AGENT_API_TOKENS", " tok1 , tok2 , tok3 ")
        monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
        tokens = get_api_tokens()
        assert tokens == ["tok1", "tok2", "tok3"]

    def test_empty_segments_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty segments from trailing commas are skipped."""
        monkeypatch.setenv("AGENT_API_TOKENS", "tok1,,tok2,")
        monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
        tokens = get_api_tokens()
        assert tokens == ["tok1", "tok2"]


# ===========================================================================
# Task 1: Multi-token support — BearerAuthMiddleware
# ===========================================================================


class TestBearerAuthMiddlewareMultiToken:
    """Tests for BearerAuthMiddleware with multiple tokens."""

    def test_first_token_accepted(self) -> None:
        """Request with the first valid token returns 200."""
        app = _app_with_bearer_middleware(tokens=["alpha", "beta"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer alpha"})
        assert response.status_code == 200

    def test_second_token_accepted(self) -> None:
        """Request with the second valid token returns 200."""
        app = _app_with_bearer_middleware(tokens=["alpha", "beta"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer beta"})
        assert response.status_code == 200

    def test_invalid_token_rejected(self) -> None:
        """Request with an unknown token returns 401."""
        app = _app_with_bearer_middleware(tokens=["alpha", "beta"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    def test_missing_authorization_header(self) -> None:
        """Request without Authorization header returns 401."""
        app = _app_with_bearer_middleware(tokens=["alpha"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test")
        assert response.status_code == 401

    def test_empty_tokens_disables_auth(self) -> None:
        """Empty token list disables auth — all requests pass through."""
        app = _app_with_bearer_middleware(tokens=[])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test")
        assert response.status_code == 200

    def test_none_tokens_disables_auth(self) -> None:
        """None tokens disables auth — all requests pass through."""
        app = _app_with_bearer_middleware(tokens=None)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test")
        assert response.status_code == 200

    def test_exempt_path_bypasses_auth(self) -> None:
        """Exempt paths (e.g., /healthz) bypass token validation."""
        app = _app_with_bearer_middleware(tokens=["secret"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_caller_identity_set_on_valid_token(self) -> None:
        """Valid token sets caller_identity on request state."""
        app = FastAPI()

        captured_identity: list = []

        @app.get("/check")
        async def check_identity(request: Request):
            identity = getattr(request.state, "caller_identity", None)
            if identity:
                captured_identity.append(identity)
            return {"ok": True}

        app.add_middleware(BearerAuthMiddleware, tokens=["mytoken"])
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/check", headers={"Authorization": "Bearer mytoken"})
        assert len(captured_identity) == 1
        assert captured_identity[0].auth_mode == "shared_secret"


# ===========================================================================
# Task 1: Backward compatibility — old single token constructor
# ===========================================================================


class TestBackwardCompatibility:
    """Tests that old single-token AGENT_API_TOKEN still works end-to-end."""

    def test_get_api_token_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_api_token() (singular) still returns AGENT_API_TOKEN."""
        monkeypatch.setenv("AGENT_API_TOKEN", "old-token")
        assert get_api_token() == "old-token"

    def test_legacy_single_token_accepted_via_get_api_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy single AGENT_API_TOKEN appears in get_api_tokens() result."""
        monkeypatch.delenv("AGENT_API_TOKENS", raising=False)
        monkeypatch.setenv("AGENT_API_TOKEN", "legacy")
        tokens = get_api_tokens()
        assert "legacy" in tokens


# ===========================================================================
# Task 2: Rejected token logging
# ===========================================================================


class TestRejectedTokenLogging:
    """Tests that rejected tokens are logged with prefix, not full token."""

    def test_rejected_token_logs_warning_with_prefix(self, caplog: Any) -> None:
        """Rejected token produces a warning log with token prefix."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)
        with caplog.at_level(logging.WARNING, logger="cloud_agents.runtime.auth"):
            client.get("/test", headers={"Authorization": "Bearer badtoken1234"})

        # Should log with prefix (first 4 chars) but NOT the full token
        assert any("badt" in record.message for record in caplog.records), (
            f"Expected token prefix 'badt' in log, got: {[r.message for r in caplog.records]}"
        )
        assert not any("badtoken1234" in record.message for record in caplog.records), (
            "Full token should NOT appear in log messages"
        )

    def test_missing_header_does_not_log_token_prefix(self, caplog: Any) -> None:
        """Missing Authorization header does not attempt to log a token prefix."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)
        with caplog.at_level(logging.WARNING, logger="cloud_agents.runtime.auth"):
            client.get("/test")

        # No prefix logging when header is missing entirely
        assert all("prefix" not in record.message.lower() for record in caplog.records)


# ===========================================================================
# Task 2: Rejected token audit event
# ===========================================================================


class TestRejectedTokenAudit:
    """Tests that rejected tokens produce audit events."""

    def test_rejected_token_emits_audit_event(self) -> None:
        """Rejected bearer token calls emit_audit with auth_rejected type."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)

        with patch("cloud_agents.runtime.auth.emit_audit") as mock_audit:
            client.get("/test", headers={"Authorization": "Bearer wrong-token"})
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args
            # Can be positional or keyword — check event_type
            if call_kwargs.kwargs:
                assert call_kwargs.kwargs.get("event_type") == "auth_rejected"
            else:
                assert call_kwargs.args[0] == "auth_rejected"

    def test_audit_event_contains_token_prefix(self) -> None:
        """Audit event details include the rejected token prefix."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)

        with patch("cloud_agents.runtime.auth.emit_audit") as mock_audit:
            client.get("/test", headers={"Authorization": "Bearer badx9999"})
            mock_audit.assert_called_once()
            details = mock_audit.call_args.kwargs.get(
                "details", mock_audit.call_args[1].get("details", {}) if len(mock_audit.call_args) > 1 else {}
            )
            assert details.get("token_prefix") == "badx"

    def test_audit_event_contains_request_path(self) -> None:
        """Audit event details include the request path."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)

        with patch("cloud_agents.runtime.auth.emit_audit") as mock_audit:
            client.get("/test", headers={"Authorization": "Bearer wrong"})
            mock_audit.assert_called_once()
            details = mock_audit.call_args.kwargs.get(
                "details", mock_audit.call_args[1].get("details", {}) if len(mock_audit.call_args) > 1 else {}
            )
            assert details.get("path") == "/test"

    def test_valid_token_does_not_emit_audit(self) -> None:
        """Valid token does not emit an auth_rejected audit event."""
        app = _app_with_bearer_middleware(tokens=["valid-token"])
        client = TestClient(app, raise_server_exceptions=False)

        with patch("cloud_agents.runtime.auth.emit_audit") as mock_audit:
            client.get("/test", headers={"Authorization": "Bearer valid-token"})
            mock_audit.assert_not_called()


# ===========================================================================
# Task 2: Audit event type — auth_rejected in AuditEventType
# ===========================================================================


class TestAuditEventType:
    """Tests that auth_rejected is a valid AuditEventType."""

    def test_auth_rejected_is_valid_event_type(self) -> None:
        """AuditEvent accepts auth_rejected as event_type."""
        event = AuditEvent(
            event_type="auth_rejected",
            workflow_id="",
        )
        assert event.event_type == "auth_rejected"

    def test_emit_audit_with_optional_workflow_id(self) -> None:
        """emit_audit() works without workflow_id for pre-workflow events."""
        from cloud_agents.workflow.audit import emit_audit

        event = emit_audit(event_type="auth_rejected", details={"token_prefix": "test"})
        assert event.event_type == "auth_rejected"
        assert event.workflow_id == ""


# ===========================================================================
# Task 3: Token expiry
# ===========================================================================


class TestTokenExpiry:
    """Tests for optional token expiry via timestamp suffix."""

    def test_non_expired_token_accepted(self) -> None:
        """Token with future timestamp is accepted."""
        future_ts = str(int(time.time()) + 3600)  # 1 hour from now
        raw_token = f"mytoken:{future_ts}"
        app = _app_with_bearer_middleware(tokens=[raw_token])
        client = TestClient(app, raise_server_exceptions=False)
        # Client sends just the token part, without the timestamp
        response = client.get("/test", headers={"Authorization": "Bearer mytoken"})
        assert response.status_code == 200

    def test_expired_token_rejected(self) -> None:
        """Token with past timestamp is rejected."""
        past_ts = str(int(time.time()) - 3600)  # 1 hour ago
        raw_token = f"mytoken:{past_ts}"
        app = _app_with_bearer_middleware(tokens=[raw_token])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer mytoken"})
        assert response.status_code == 401

    def test_plain_token_no_expiry(self) -> None:
        """Token without timestamp suffix never expires."""
        app = _app_with_bearer_middleware(tokens=["plain-token"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer plain-token"})
        assert response.status_code == 200

    def test_token_with_non_numeric_suffix_no_expiry(self) -> None:
        """Token with non-numeric colon suffix is treated as plain token."""
        # "token:abc" — abc is not a valid timestamp, so entire string is the token
        app = _app_with_bearer_middleware(tokens=["token:abc"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer token:abc"})
        assert response.status_code == 200

    def test_mixed_expiry_and_plain_tokens(self) -> None:
        """Mix of tokens with and without expiry works correctly."""
        future_ts = str(int(time.time()) + 3600)
        past_ts = str(int(time.time()) - 3600)
        tokens = [
            "plain-token",
            f"future-token:{future_ts}",
            f"expired-token:{past_ts}",
        ]
        app = _app_with_bearer_middleware(tokens=tokens)
        client = TestClient(app, raise_server_exceptions=False)

        # Plain token → accepted
        assert client.get("/test", headers={"Authorization": "Bearer plain-token"}).status_code == 200
        # Future token → accepted
        assert client.get("/test", headers={"Authorization": "Bearer future-token"}).status_code == 200
        # Expired token → rejected
        assert client.get("/test", headers={"Authorization": "Bearer expired-token"}).status_code == 401

    def test_expired_token_emits_audit_event(self) -> None:
        """Expired token produces audit event with reason=expired."""
        past_ts = str(int(time.time()) - 3600)
        raw_token = f"exptoken:{past_ts}"
        app = _app_with_bearer_middleware(tokens=[raw_token])
        client = TestClient(app, raise_server_exceptions=False)

        with patch("cloud_agents.runtime.auth.emit_audit") as mock_audit:
            client.get("/test", headers={"Authorization": "Bearer exptoken"})
            mock_audit.assert_called_once()
            details = mock_audit.call_args.kwargs.get(
                "details", mock_audit.call_args[1].get("details", {}) if len(mock_audit.call_args) > 1 else {}
            )
            assert details.get("reason") == "expired"


# ===========================================================================
# Task 4: create_bearer_auth_dependency
# ===========================================================================


class TestCreateBearerAuthDependency:
    """Tests for the FastAPI dependency function factory."""

    def test_valid_token_passes(self) -> None:
        """Valid token passes dependency check and returns 200."""
        app = _app_with_dependency(tokens=["dep-token"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer dep-token"})
        assert response.status_code == 200

    def test_invalid_token_returns_401(self) -> None:
        """Invalid token causes dependency to raise 401."""
        app = _app_with_dependency(tokens=["dep-token"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    def test_missing_bearer_prefix_returns_401(self) -> None:
        """Authorization header without Bearer prefix returns 401."""
        app = _app_with_dependency(tokens=["dep-token"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Basic dep-token"})
        assert response.status_code == 401

    def test_sets_caller_identity(self) -> None:
        """Dependency sets caller_identity on request state."""
        app = _app_with_dependency(tokens=["dep-token"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer dep-token"})
        assert response.status_code == 200
        body = response.json()
        assert body.get("username") == "anonymous"

    def test_non_protected_route_not_affected(self) -> None:
        """Routes outside the protected router are not affected."""
        app = _app_with_dependency(tokens=["dep-token"])
        client = TestClient(app, raise_server_exceptions=False)
        # /healthz is not on the protected router
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_multi_token_accepted(self) -> None:
        """Multiple tokens are all valid via dependency."""
        app = _app_with_dependency(tokens=["t1", "t2", "t3"])
        client = TestClient(app, raise_server_exceptions=False)
        for tok in ["t1", "t2", "t3"]:
            response = client.get("/test", headers={"Authorization": f"Bearer {tok}"})
            assert response.status_code == 200, f"Token '{tok}' should be accepted"

    def test_expired_token_rejected_via_dependency(self) -> None:
        """Expired token rejected when used through dependency."""
        past_ts = str(int(time.time()) - 3600)
        app = _app_with_dependency(tokens=[f"dep-token:{past_ts}"])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={"Authorization": "Bearer dep-token"})
        assert response.status_code == 401


# ===========================================================================
# Task 4: _parse_token_map internal helper
# ===========================================================================


class TestParseTokenMap:
    """Tests for _parse_token_map() internal helper."""

    def test_plain_tokens(self) -> None:
        """Plain tokens without timestamp have None expiry."""
        from cloud_agents.runtime.auth import _parse_token_map

        result = _parse_token_map(["tok1", "tok2"])
        assert result == {"tok1": None, "tok2": None}

    def test_token_with_valid_timestamp(self) -> None:
        """Token with numeric suffix is parsed as token:expiry pair."""
        from cloud_agents.runtime.auth import _parse_token_map

        result = _parse_token_map(["mytoken:1735689600"])
        assert "mytoken" in result
        assert result["mytoken"] == 1735689600.0

    def test_token_with_non_numeric_suffix(self) -> None:
        """Token with non-numeric suffix is kept as-is with no expiry."""
        from cloud_agents.runtime.auth import _parse_token_map

        result = _parse_token_map(["mytoken:abc"])
        assert "mytoken:abc" in result
        assert result["mytoken:abc"] is None

    def test_empty_list(self) -> None:
        """Empty token list returns empty map."""
        from cloud_agents.runtime.auth import _parse_token_map

        result = _parse_token_map([])
        assert result == {}

    def test_token_with_multiple_colons(self) -> None:
        """Token with multiple colons uses rsplit — last segment is timestamp."""
        from cloud_agents.runtime.auth import _parse_token_map

        result = _parse_token_map(["part1:part2:1735689600"])
        assert "part1:part2" in result
        assert result["part1:part2"] == 1735689600.0
