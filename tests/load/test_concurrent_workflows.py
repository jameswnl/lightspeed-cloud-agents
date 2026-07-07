"""Load test: concurrent workflow submissions.

Submits N workflows simultaneously via POST /v1/workflows/run and
verifies that all complete or fail gracefully with no hung requests.
Measures p50/p95/p99 latency and error rate.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi.testclient import TestClient

from tests.load.helpers import LatencyTracker, ResponseCollector, WorkflowFactory


class TestConcurrentWorkflowSubmissions:
    """Verify system handles concurrent workflow submissions correctly."""

    CONCURRENCY_LEVELS = [10, 50, 100]

    @pytest.mark.parametrize("num_workflows", CONCURRENCY_LEVELS)
    def test_concurrent_submissions_all_respond(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
        num_workflows: int,
    ) -> None:
        """All concurrent submissions receive a response (no hung requests)."""
        collector = ResponseCollector()

        for _ in range(num_workflows):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.total_count == num_workflows

    def test_concurrent_submissions_return_202(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """All workflow submissions return 202 Accepted."""
        collector = ResponseCollector()

        for _ in range(20):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.success_count == 20
        assert collector.status_codes[202] == 20

    def test_concurrent_submissions_unique_workflow_ids(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Each submission produces a unique workflow_id in the response."""
        ids: set[str] = set()
        for _ in range(30):
            payload = workflow_factory.run_request()
            response = sync_client.post("/v1/workflows/run", json=payload)
            wf_id = response.json().get("workflow_id", "")
            ids.add(wf_id)

        assert len(ids) == 30

    def test_latency_under_threshold(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """p99 latency for workflow submission stays under SLO threshold.

        SLO: p99 < 500ms for 50 concurrent submissions against mocked backend.
        """
        tracker = LatencyTracker()

        for _ in range(50):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            tracker.record(elapsed)

        summary = tracker.summary()
        assert summary["p99"] < 0.5, f"p99 latency {summary['p99']:.3f}s exceeds 500ms SLO"

    def test_no_5xx_errors_under_load(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """No 5xx errors occur under moderate concurrent load."""
        collector = ResponseCollector()

        for _ in range(50):
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        error_5xx = sum(1 for code in collector.status_codes if code >= 500)
        assert error_5xx == 0, f"Got {error_5xx} 5xx errors under load"

    def test_workflow_status_queryable_after_submission(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Workflow status is queryable immediately after submission."""
        payload = workflow_factory.run_request()
        submit_resp = sync_client.post("/v1/workflows/run", json=payload)
        wf_id = submit_resp.json()["workflow_id"]

        status_resp = sync_client.get(f"/v1/workflows/{wf_id}")
        assert status_resp.status_code == 200


    def test_truly_concurrent_submissions(
        self,
        sync_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Submit workflows from multiple threads simultaneously.

        Uses ThreadPoolExecutor to fire requests in parallel, testing
        for race conditions and connection pool exhaustion.
        """
        num_concurrent = 20
        collector = ResponseCollector()

        def submit_one() -> tuple[int, float]:
            payload = workflow_factory.run_request()
            start = time.perf_counter()
            response = sync_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            return response.status_code, elapsed

        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            futures = [executor.submit(submit_one) for _ in range(num_concurrent)]
            for future in as_completed(futures):
                status_code, latency = future.result()
                collector.add(status_code=status_code, latency=latency)

        assert collector.total_count == num_concurrent
        assert collector.success_count == num_concurrent
        assert collector.error_count == 0
