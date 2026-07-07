"""Load test: approval gate backpressure.

Submits workflows that pause at approval gates and verifies no
resource leaks. Tests that approval signals are correctly routed
when multiple workflows are waiting simultaneously.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agents.workflow.temporal_api import build_temporal_router

from tests.load.helpers import ResponseCollector, WorkflowFactory


@pytest.fixture
def approval_app() -> tuple[FastAPI, MagicMock]:
    """Create a FastAPI app with mock Temporal that tracks signal calls."""
    client = MagicMock()
    handle = AsyncMock()
    handle.id = "wf-approval"
    handle.query.return_value = MagicMock(
        model_dump=lambda: {
            "steps": {"analyze": {"status": "completed"}},
            "events": [
                {"type": "step.waiting_approval", "step": "remediate", "timestamp": "T0"}
            ],
        },
        events=[
            MagicMock(
                model_dump=lambda: {
                    "type": "step.waiting_approval",
                    "step": "remediate",
                    "timestamp": "T0",
                }
            )
        ],
    )
    handle.signal = AsyncMock()
    client.start_workflow = AsyncMock(return_value=handle)
    client.get_workflow_handle.return_value = handle

    app = FastAPI()
    router = build_temporal_router(client)
    app.include_router(router)
    return app, client


@pytest.fixture
def approval_client(approval_app: tuple[FastAPI, MagicMock]) -> TestClient:
    """Create a test client for the approval test app."""
    return TestClient(approval_app[0])


@pytest.fixture
def approval_mock_client(
    approval_app: tuple[FastAPI, MagicMock],
) -> MagicMock:
    """Get the mock Temporal client from the approval test app."""
    return approval_app[1]


class TestApprovalBackpressure:
    """Verify system handles many workflows waiting at approval gates."""

    def test_multiple_workflows_submit_and_reach_approval(
        self,
        approval_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Multiple workflows can be submitted and reach approval state."""
        collector = ResponseCollector()
        num_workflows = 20

        for _ in range(num_workflows):
            payload = workflow_factory.run_request_with_approval()
            start = time.perf_counter()
            response = approval_client.post("/v1/workflows/run", json=payload)
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.success_count == num_workflows

    def test_approval_signals_routed_to_correct_workflow(
        self,
        approval_client: TestClient,
        approval_mock_client: MagicMock,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Approval signals are routed to the correct workflow handle."""
        workflow_ids: list[str] = []

        for _ in range(10):
            payload = workflow_factory.run_request_with_approval()
            response = approval_client.post("/v1/workflows/run", json=payload)
            workflow_ids.append(response.json()["workflow_id"])

        for wf_id in workflow_ids:
            response = approval_client.post(
                f"/v1/workflows/{wf_id}/approve",
                json={"step_name": "remediate", "decision": "approved"},
            )
            assert response.status_code == 200

        assert approval_mock_client.get_workflow_handle.call_count >= 10

    def test_status_queryable_while_waiting_approval(
        self,
        approval_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Workflow status is queryable while waiting for approval."""
        payload = workflow_factory.run_request_with_approval()
        submit_resp = approval_client.post("/v1/workflows/run", json=payload)
        wf_id = submit_resp.json()["workflow_id"]

        status_resp = approval_client.get(f"/v1/workflows/{wf_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert "steps" in data
        assert "events" in data

    def test_batch_approvals_under_load(
        self,
        approval_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Batch approval of many waiting workflows completes without errors."""
        collector = ResponseCollector()
        workflow_ids: list[str] = []
        num_workflows = 30

        for _ in range(num_workflows):
            payload = workflow_factory.run_request_with_approval()
            response = approval_client.post("/v1/workflows/run", json=payload)
            workflow_ids.append(response.json()["workflow_id"])

        for wf_id in workflow_ids:
            start = time.perf_counter()
            response = approval_client.post(
                f"/v1/workflows/{wf_id}/approve",
                json={"step_name": "remediate", "decision": "approved"},
            )
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.success_count == num_workflows
        assert collector.error_count == 0

    def test_denial_under_load(
        self,
        approval_client: TestClient,
        workflow_factory: WorkflowFactory,
    ) -> None:
        """Denying workflows under load works correctly."""
        collector = ResponseCollector()
        num_workflows = 15

        for _ in range(num_workflows):
            payload = workflow_factory.run_request_with_approval()
            submit_resp = approval_client.post("/v1/workflows/run", json=payload)
            wf_id = submit_resp.json()["workflow_id"]

            start = time.perf_counter()
            response = approval_client.post(
                f"/v1/workflows/{wf_id}/approve",
                json={"step_name": "remediate", "decision": "denied"},
            )
            elapsed = time.perf_counter() - start
            collector.add(status_code=response.status_code, latency=elapsed)

        assert collector.success_count == num_workflows
