"""Unit tests for Temporal entrypoint integration (TDD)."""

from __future__ import annotations

from pytest_mock import MockerFixture

from agents.workflow.temporal_entrypoint import build_temporal_app


class TestTemporalEntrypoint:
    """Tests for build_temporal_app function."""

    def test_returns_fastapi_app(self) -> None:
        """build_temporal_app returns a FastAPI application."""
        from fastapi import FastAPI
        app = build_temporal_app(temporal_url="localhost:7233")
        assert isinstance(app, FastAPI)

    def test_app_has_workflow_routes(self) -> None:
        """The app includes workflow API routes."""
        app = build_temporal_app(temporal_url="localhost:7233")
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/v1/workflows/run" in routes

    def test_app_has_health_endpoint(self) -> None:
        """The app includes a health check endpoint."""
        app = build_temporal_app(temporal_url="localhost:7233")
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/healthz" in routes

    def test_tracing_initialized_on_build(self, mocker: MockerFixture) -> None:
        """build_temporal_app calls init_tracing."""
        mock_init = mocker.patch(
            "agents.workflow.temporal_entrypoint.init_tracing",
        )
        build_temporal_app(temporal_url="localhost:7233")
        mock_init.assert_called_once_with("workflow-runner")

    def test_app_has_metrics_endpoint(self) -> None:
        """The app includes a /metrics endpoint."""
        app = build_temporal_app(temporal_url="localhost:7233")
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/metrics" in routes

    def test_tls_config_read_from_env(self, mocker: MockerFixture) -> None:
        """TLS config is built when TEMPORAL_TLS_ENABLED=true."""
        mocker.patch.dict("os.environ", {
            "TEMPORAL_TLS_ENABLED": "true",
            "TEMPORAL_TLS_CERT_PATH": "/certs/client.pem",
            "TEMPORAL_TLS_KEY_PATH": "/certs/client.key",
        })
        mocker.patch("builtins.open", mocker.mock_open(read_data=b"cert-data"))
        from agents.workflow.temporal_entrypoint import _build_tls_config
        tls = _build_tls_config()
        assert tls is not None

    def test_no_tls_by_default(self, mocker: MockerFixture) -> None:
        """TLS is disabled by default."""
        mocker.patch.dict("os.environ", {}, clear=False)
        from agents.workflow.temporal_entrypoint import _build_tls_config
        tls = _build_tls_config()
        assert tls is None

    def test_app_has_livez_endpoint(self) -> None:
        """The app includes a /livez endpoint."""
        app = build_temporal_app(temporal_url="localhost:7233")
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/livez" in routes

    def test_app_has_readyz_endpoint(self) -> None:
        """The app includes a /readyz endpoint."""
        app = build_temporal_app(temporal_url="localhost:7233")
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/readyz" in routes

    def test_livez_returns_200(self) -> None:
        """GET /livez returns 200 when process is alive."""
        from fastapi.testclient import TestClient
        app = build_temporal_app(temporal_url="localhost:7233")
        client = TestClient(app)
        response = client.get("/livez")
        assert response.status_code == 200

    def test_metrics_returns_prometheus_format(self) -> None:
        """GET /metrics returns Prometheus exposition format."""
        from fastapi.testclient import TestClient
        app = build_temporal_app(temporal_url="localhost:7233")
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "ls_workflow" in response.text or "python_info" in response.text
