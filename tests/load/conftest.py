"""Shared fixtures for load and stress tests.

Provides mock Temporal clients, FastAPI test apps, and workflow
payload generators for load test scenarios.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.workflow.temporal_api import build_temporal_router

from tests.load.helpers import LatencyTracker, ResponseCollector, WorkflowFactory


@pytest.fixture
def workflow_factory() -> WorkflowFactory:
    """Create a WorkflowFactory for generating request payloads."""
    return WorkflowFactory(id_prefix="load-test")


@pytest.fixture
def latency_tracker() -> LatencyTracker:
    """Create a LatencyTracker for recording request latencies."""
    return LatencyTracker()


@pytest.fixture
def response_collector() -> ResponseCollector:
    """Create a ResponseCollector for aggregating results."""
    return ResponseCollector()


@pytest.fixture
def mock_temporal_client() -> Any:
    """Create a mock Temporal client that accepts workflow starts."""
    client = MagicMock()
    handle = AsyncMock()
    handle.id = "wf-load-test"
    handle.query.return_value = MagicMock(
        model_dump=lambda: {"steps": {}, "events": []},
        events=[],
    )
    client.start_workflow = AsyncMock(return_value=handle)
    client.get_workflow_handle.return_value = handle
    return client


def _add_health_endpoints(app: FastAPI) -> None:
    """Add health/liveness/readiness endpoints to a test app."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}


@pytest.fixture
def load_test_app(mock_temporal_client: Any) -> FastAPI:
    """Create a FastAPI app with mocked Temporal for load testing."""
    app = FastAPI()
    router = build_temporal_router(mock_temporal_client)
    app.include_router(router)
    _add_health_endpoints(app)
    return app


@pytest.fixture
def rate_limited_app(mock_temporal_client: Any) -> FastAPI:
    """Create a FastAPI app with rate limiting enabled.

    Rate: 10 req/s, burst: 20. Allows testing rate limiter under load.
    """
    from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

    app = FastAPI()
    router = build_temporal_router(mock_temporal_client)
    app.include_router(router)
    _add_health_endpoints(app)
    app.add_middleware(RateLimitMiddleware, rate=10.0, burst=20)
    return app


@pytest.fixture
def sync_client(load_test_app: FastAPI) -> TestClient:
    """Create a synchronous TestClient for the load test app."""
    return TestClient(load_test_app)


@pytest.fixture
def rate_limited_client(rate_limited_app: FastAPI) -> TestClient:
    """Create a synchronous TestClient for the rate-limited app."""
    return TestClient(rate_limited_app)
