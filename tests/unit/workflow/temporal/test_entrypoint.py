"""Unit tests for Temporal entrypoint integration (TDD)."""

from __future__ import annotations

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
