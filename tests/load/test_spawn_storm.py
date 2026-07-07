"""Load test: sandbox spawn storm.

Submits workflows exceeding MAX_SPAWNED_PODS to verify queuing
behavior and graceful degradation.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.load.helpers import LatencyTracker, ResponseCollector, WorkflowFactory


class TestSpawnStorm:
    """Verify system degrades gracefully when spawn capacity is exceeded."""

    def test_submissions_above_max_all_accepted_by_api(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """API accepts all submissions even when exceeding spawn capacity."""
        collector = ResponseCollector()
        num_workflows = 100

        for _ in range(num_workflows):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.status_codes[202] == num_workflows

    def test_submission_latency_stable_under_volume(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Submission latency remains stable as volume increases."""
        tracker_early = LatencyTracker()
        tracker_late = LatencyTracker()

        for i in range(100):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start

            if i < 10:
                tracker_early.record(elapsed)
            elif i >= 90:
                tracker_late.record(elapsed)

        p50_early = tracker_early.percentile(50)
        p50_late = tracker_late.percentile(50)

        assert p50_late < p50_early * 3, (
            f"Latency degradation: early p50={p50_early*1000:.1f}ms, "
            f"late p50={p50_late*1000:.1f}ms"
        )

    def test_status_queries_work_during_storm(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Workflow status queries succeed even during high submission volume."""
        workflow_ids: list[str] = []

        for _ in range(50):
            payload = workflow_factory.run_request()
            response = sync_client.post("/v1/workflows/run", json=payload)
            workflow_ids.append(response.json()["workflow_id"])

        collector = ResponseCollector()
        for wf_id in workflow_ids:
            start = time.perf_counter()
            response = sync_client.get(f"/v1/workflows/{wf_id}")
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.success_count == 50
        assert collector.error_count == 0

    def test_mixed_operations_during_storm(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Mixed submit + query operations work correctly under volume."""
        collector = ResponseCollector()
        last_wf_id: str | None = None

        for i in range(60):
            if i % 3 == 0 and last_wf_id:
                start = time.perf_counter()
                response = sync_client.get(f"/v1/workflows/{last_wf_id}")
                elapsed = time.perf_counter() - start
                collector.add(status_code=response.status_code, latency=elapsed)
            else:
                payload = workflow_factory.run_request()
                start = time.perf_counter()
                response = sync_client.post("/v1/workflows/run", json=payload)
                elapsed = time.perf_counter() - start
                collector.add(status_code=response.status_code, latency=elapsed)
                last_wf_id = response.json().get("workflow_id")

        assert collector.error_count == 0
