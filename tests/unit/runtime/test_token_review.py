"""Unit tests for TokenReview auth middleware (Phase 8 Task 10)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.runtime.auth import TokenReviewAuthMiddleware, get_auth_mode


def _make_app_with_token_review() -> TestClient:
    """Create a test app with TokenReview middleware."""
    app = FastAPI()
    app.add_middleware(TokenReviewAuthMiddleware)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ready"}

    @app.get("/v1/test")
    async def test_endpoint():
        return {"ok": True}

    return TestClient(app)


class TestTokenReviewAuth:
    """Tests for TokenReviewAuthMiddleware."""

    def test_healthz_exempt(self) -> None:
        """Healthz is accessible without token."""
        tc = _make_app_with_token_review()
        resp = tc.get("/healthz")
        assert resp.status_code == 200

    def test_missing_token_returns_401(self) -> None:
        """No bearer token returns 401."""
        tc = _make_app_with_token_review()
        resp = tc.get("/v1/test")
        assert resp.status_code == 401

    def test_valid_token_accepted(self) -> None:
        """Valid token passes through."""
        import sys

        mock_k8s_client = MagicMock()
        mock_auth_api = MagicMock()
        mock_result = MagicMock()
        mock_result.status.authenticated = True
        mock_auth_api.create_token_review.return_value = mock_result
        mock_k8s_client.AuthenticationV1Api.return_value = mock_auth_api

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = MagicMock()

        tc = _make_app_with_token_review()
        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s.config,
        }):
            resp = tc.get("/v1/test", headers={"Authorization": "Bearer valid-sa-token"})

        assert resp.status_code == 200

    def test_invalid_token_rejected(self) -> None:
        """Invalid token returns 401."""
        import sys

        mock_k8s_client = MagicMock()
        mock_auth_api = MagicMock()
        mock_result = MagicMock()
        mock_result.status.authenticated = False
        mock_auth_api.create_token_review.return_value = mock_result
        mock_k8s_client.AuthenticationV1Api.return_value = mock_auth_api

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = MagicMock()

        tc = _make_app_with_token_review()
        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s.config,
        }):
            resp = tc.get("/v1/test", headers={"Authorization": "Bearer bad-token"})

        assert resp.status_code == 401


class TestGetAuthMode:
    """Tests for auth mode selection."""

    def test_default_is_shared_secret(self) -> None:
        """Default auth mode is shared_secret."""
        import os
        with patch.dict(os.environ, {}, clear=True):
            assert get_auth_mode() == "shared_secret"

    def test_sa_token_mode(self) -> None:
        """AUTH_MODE=sa_token selects token review."""
        import os
        with patch.dict(os.environ, {"AUTH_MODE": "sa_token"}):
            assert get_auth_mode() == "sa_token"
