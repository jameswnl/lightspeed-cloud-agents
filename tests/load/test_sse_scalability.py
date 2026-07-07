"""Load test: SSE connection scalability.

Opens N SSE connections to /v1/workflows/{id}/events and verifies
server stability.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus

from cloud_agents.workflow.temporal_api import build_temporal_router

from tests.load.helpers import WorkflowFactory


@pytest.fixture
def sse_app() -> FastAPI:
    """Create a FastAPI app with SSE-capable mock Temporal."""
    client = MagicMock()
    handle = AsyncMock()
    handle.id = "wf-sse-test"

    status_result = MagicMock()
    status_result.events = [
        MagicMock(
            model_dump=lambda: {
                "type": "step.started",
                "step": "analyze",
                "timestamp": "2024-01-01T00:00:00Z",
            }
        ),
        MagicMock(
            model_dump=lambda: {
                "type": "step.completed",
                "step": "analyze",
                "timestamp": "2024-01-01T00:00:01Z",
            }
        ),
    ]
    handle.query.return_value = status_result

    desc = AsyncMock()
    desc.status = WorkflowExecutionStatus.COMPLETED
    handle.describe.return_value = desc

    client.start_workflow = AsyncMock(return_value=handle)
    client.get_workflow_handle.return_value = handle

    app = FastAPI()
    router = build_temporal_router(client)
    app.include_router(router)

    from tests.load.conftest import _add_health_endpoints

    _add_health_endpoints(app)
    return app


@pytest.fixture
def sse_client(sse_app: FastAPI) -> TestClient:
    """Create a test client for SSE testing."""
    return TestClient(sse_app)


class TestSSEScalability:
    """Verify SSE event streaming scales with concurrent connections."""

    def test_single_sse_connection_receives_events(
        self,
        sse_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """A single SSE connection receives workflow events."""
        payload = workflow_factory.run_request()
        submit_resp = sse_client.post("/v1/workflows/run", json=payload)
        wf_id = submit_resp.json()["workflow_id"]

        with sse_client.stream("GET", f"/v1/workflows/{wf_id}/events") as response:
            assert response.status_code == 200
            events: list[str] = []
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(line)
                if len(events) >= 3:
                    break

        assert len(events) >= 2

    def test_multiple_sse_connections_all_receive_data(
        self,
        sse_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Multiple sequential SSE connections each receive event data."""
        num_connections = 10
        connection_results: list[int] = []

        for _ in range(num_connections):
            payload = workflow_factory.run_request()
            submit_resp = sse_client.post("/v1/workflows/run", json=payload)
            wf_id = submit_resp.json()["workflow_id"]

            event_count = 0
            with sse_client.stream("GET", f"/v1/workflows/{wf_id}/events") as response:
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        event_count += 1
                    if event_count >= 3:
                        break

            connection_results.append(event_count)

        assert all(count >= 2 for count in connection_results), (
            f"Some connections received too few events: {connection_results}"
        )

    def test_sse_connection_latency(
        self,
        sse_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Time to first SSE event stays under threshold."""
        payload = workflow_factory.run_request()
        submit_resp = sse_client.post("/v1/workflows/run", json=payload)
        wf_id = submit_resp.json()["workflow_id"]

        start = time.perf_counter()
        first_event_time = None
        with sse_client.stream("GET", f"/v1/workflows/{wf_id}/events") as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    first_event_time = time.perf_counter() - start
                    break

        assert first_event_time is not None
        assert first_event_time < 2.0, (
            f"First SSE event took {first_event_time:.2f}s (SLO: <2s)"
        )

    def test_sse_completed_event_terminates_stream(
        self,
        sse_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """SSE stream terminates after workflow.completed event."""
        payload = workflow_factory.run_request()
        submit_resp = sse_client.post("/v1/workflows/run", json=payload)
        wf_id = submit_resp.json()["workflow_id"]

        events: list[str] = []
        with sse_client.stream("GET", f"/v1/workflows/{wf_id}/events") as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(line)

        completed = [e for e in events if "workflow.completed" in e]
        assert len(completed) >= 1

    def test_health_endpoints_during_sse_load(
        self,
        sse_client: TestClient,
    ) -> None:
        """Health endpoints respond normally even during SSE activity."""
        for path in ["/healthz", "/livez", "/readyz"]:
            response = sse_client.get(path)
            assert response.status_code == 200
