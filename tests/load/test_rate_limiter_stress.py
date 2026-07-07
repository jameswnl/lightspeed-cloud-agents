"""Load test: rate limiter stress.

Bursts requests above RATE_LIMIT_BURST from a single caller and
verifies correct 429 responses with Retry-After headers.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.load.helpers import LatencyTracker, ResponseCollector, WorkflowFactory


class TestRateLimiterUnderLoad:
    """Verify rate limiter enforces limits without affecting other callers."""

    def test_burst_within_limit_all_accepted(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Requests within burst limit are all accepted (200/202)."""
        collector = ResponseCollector()

        for _ in range(20):
            payload = workflow_factory.run_request()
            response = rate_limited_client.post("/v1/workflows/run", json=payload)
            collector.add(status_code=response.status_code, latency=0.0)

        assert collector.success_count == 20

    def test_burst_above_limit_returns_429(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Requests above burst limit return 429 Too Many Requests."""
        for _ in range(20):
            payload = workflow_factory.run_request()
            rate_limited_client.post("/v1/workflows/run", json=payload)

        payload = workflow_factory.run_request()
        response = rate_limited_client.post("/v1/workflows/run", json=payload)
        assert response.status_code == 429

    def test_429_includes_retry_after_header(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Rate-limited responses include Retry-After header."""
        for _ in range(20):
            payload = workflow_factory.run_request()
            rate_limited_client.post("/v1/workflows/run", json=payload)

        payload = workflow_factory.run_request()
        response = rate_limited_client.post("/v1/workflows/run", json=payload)
        assert response.status_code == 429
        assert "retry-after" in response.headers
        retry_after = int(response.headers["retry-after"])
        assert retry_after >= 1

    def test_different_callers_independent_limits(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Rate limiting is per-caller; exhausting one caller does not affect another."""
        for _ in range(20):
            payload = workflow_factory.run_request()
            rate_limited_client.post(
                "/v1/workflows/run",
                json=payload,
                headers={"Authorization": "Bearer caller-a-token"},
            )

        payload = workflow_factory.run_request()
        resp_a = rate_limited_client.post(
            "/v1/workflows/run",
            json=payload,
            headers={"Authorization": "Bearer caller-a-token"},
        )
        assert resp_a.status_code == 429

        payload = workflow_factory.run_request()
        resp_b = rate_limited_client.post(
            "/v1/workflows/run",
            json=payload,
            headers={"Authorization": "Bearer caller-b-token"},
        )
        assert resp_b.status_code == 202

    def test_sustained_burst_produces_expected_rejection_ratio(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Under sustained burst, rejection ratio matches expected rate."""
        collector = ResponseCollector()

        for _ in range(40):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = rate_limited_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.status_codes[202] >= 18, "Too few requests accepted"
        assert collector.status_codes[429] >= 15, "Too few requests rejected"

    def test_health_endpoints_never_rate_limited(
        self,
        rate_limited_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Health endpoints are exempt from rate limiting even under burst."""
        for _ in range(25):
            payload = workflow_factory.run_request()
            rate_limited_client.post("/v1/workflows/run", json=payload)

        for path in ["/healthz", "/livez", "/readyz"]:
            response = rate_limited_client.get(path)
            assert response.status_code == 200, f"{path} was rate limited"

    def test_rate_limiter_latency_overhead_minimal(
        self,
        rate_limited_client: TestClient,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Rate limiter adds minimal latency overhead vs unprotected endpoint."""
        tracker_limited = LatencyTracker()
        tracker_unlimited = LatencyTracker()

        for _ in range(20):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            rate_limited_client.post("/v1/workflows/run", json=payload)
            tracker_limited.record(time.perf_counter() - start)

        for _ in range(20):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            sync_client.post("/v1/workflows/run", json=payload)
            tracker_unlimited.record(time.perf_counter() - start)

        overhead = tracker_limited.percentile(50) - tracker_unlimited.percentile(50)
        assert overhead < 0.005, f"Rate limiter overhead {overhead*1000:.1f}ms exceeds 5ms"
