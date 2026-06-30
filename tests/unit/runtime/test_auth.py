"""Unit tests for bearer auth middleware."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.runtime.auth import BearerAuthMiddleware


def _make_app(token: str = "test-token") -> FastAPI:
    """Create a test app with auth middleware."""
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, token=token)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ready"}

    @app.get("/livez")
    async def livez():
        return {"status": "alive"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": "data"}

    @app.post("/v1/run")
    async def run():
        return {"result": "ok"}

    @app.get("/v1/runs/abc")
    async def get_run():
        return {"run_id": "abc"}

    return app


class TestExemptPaths:
    """Tests for paths that bypass auth."""

    def test_healthz_no_auth_required(self) -> None:
        """Test /healthz is accessible without a token."""
        client = TestClient(_make_app())
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_livez_no_auth_required(self) -> None:
        """Test /livez is accessible without a token."""
        client = TestClient(_make_app())
        resp = client.get("/livez")
        assert resp.status_code == 200

    def test_metrics_no_auth_required(self) -> None:
        """Test /metrics is accessible without a token."""
        client = TestClient(_make_app())
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestProtectedPaths:
    """Tests for paths that require auth."""

    def test_run_requires_token(self) -> None:
        """Test /v1/run requires authorization."""
        client = TestClient(_make_app())
        resp = client.post("/v1/run")
        assert resp.status_code == 401

    def test_run_with_valid_token(self) -> None:
        """Test /v1/run succeeds with valid token."""
        client = TestClient(_make_app())
        resp = client.post("/v1/run", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200

    def test_run_with_invalid_token(self) -> None:
        """Test /v1/run rejects invalid token."""
        client = TestClient(_make_app())
        resp = client.post("/v1/run", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_get_run_requires_token(self) -> None:
        """Test /v1/runs/{id} requires authorization."""
        client = TestClient(_make_app())
        resp = client.get("/v1/runs/abc")
        assert resp.status_code == 401

    def test_get_run_with_valid_token(self) -> None:
        """Test /v1/runs/{id} succeeds with valid token."""
        client = TestClient(_make_app())
        resp = client.get(
            "/v1/runs/abc", headers={"Authorization": "Bearer test-token"}
        )
        assert resp.status_code == 200


class TestAuthDisabled:
    """Tests for when auth is disabled (empty token)."""

    def test_empty_token_allows_all(self) -> None:
        """Test that empty token disables auth entirely."""
        client = TestClient(_make_app(token=""))
        resp = client.post("/v1/run")
        assert resp.status_code == 200

    def test_no_token_env_allows_all(self) -> None:
        """Test backward compatibility — no token = no auth."""
        client = TestClient(_make_app(token=""))
        resp = client.get("/v1/runs/abc")
        assert resp.status_code == 200
