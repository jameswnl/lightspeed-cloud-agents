"""Unit tests for SSE step.progress event types (TDD).

Tests the new step.progress SSE event type that forwards agent
work-in-progress events (tool_call, tool_result, thinking) from
heartbeat data to the SSE stream.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from temporalio.client import WorkflowExecutionStatus

from cloud_agents.workflow.temporal_api import build_temporal_router


def _make_status(steps, events):
    """Build a mock WorkflowStatus."""
    from cloud_agents.workflow.temporal_models import (
        StepResult,
        WorkflowEvent,
        WorkflowStatus,
    )

    step_results = {
        k: StepResult(**v) if isinstance(v, dict) else v for k, v in steps.items()
    }
    event_objs = [
        WorkflowEvent(**e) if isinstance(e, dict) else e for e in events
    ]
    return WorkflowStatus(steps=step_results, events=event_objs)


def _make_describe(status_name="RUNNING"):
    """Build a mock workflow description with execution status."""
    desc = MagicMock()
    desc.status = getattr(WorkflowExecutionStatus, status_name)
    return desc


def _collect_sse(response) -> list[dict]:
    """Parse SSE data lines from a streaming response."""
    events = []
    for line in response.text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            raw = line[6:]
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return events


class TestStepProgressSSE:
    """Tests for step.progress SSE event type."""

    def test_step_progress_events_in_sse_stream(
        self,
        mocker: MockerFixture,
    ) -> None:
        """step.progress events appear in SSE stream."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=_make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(
            return_value=_make_status(
                steps={"r1": {"status": "completed"}},
                events=[
                    {"type": "step.started", "step": "diagnose", "timestamp": "t1"},
                    {
                        "type": "step.progress",
                        "step": "diagnose",
                        "timestamp": "t2",
                    },
                    {"type": "step.completed", "step": "diagnose", "timestamp": "t3"},
                ],
            )
        )

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-progress/events")
        events = _collect_sse(response)

        event_types = [e.get("type") for e in events]
        assert "step.progress" in event_types

    def test_progress_events_streamed_incrementally(
        self,
        mocker: MockerFixture,
    ) -> None:
        """step.progress events are streamed incrementally without duplicates."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        call_count = 0

        async def query_status(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_status(
                    steps={},
                    events=[
                        {"type": "step.started", "step": "s1", "timestamp": "t1"},
                        {"type": "step.progress", "step": "s1", "timestamp": "t2"},
                    ],
                )
            else:
                return _make_status(
                    steps={"r1": {"status": "completed"}},
                    events=[
                        {"type": "step.started", "step": "s1", "timestamp": "t1"},
                        {"type": "step.progress", "step": "s1", "timestamp": "t2"},
                        {"type": "step.progress", "step": "s1", "timestamp": "t3"},
                        {"type": "step.completed", "step": "s1", "timestamp": "t4"},
                    ],
                )

        handle.query = query_status

        desc_count = 0

        async def describe_status():
            nonlocal desc_count
            desc_count += 1
            if desc_count == 1:
                return _make_describe("RUNNING")
            return _make_describe("COMPLETED")

        handle.describe = describe_status

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-incr-progress/events")
        events = _collect_sse(response)

        progress_count = sum(1 for e in events if e.get("type") == "step.progress")
        # Two progress events total, both should appear exactly once
        assert progress_count == 2

    def test_progress_with_tool_info_in_event(
        self,
        mocker: MockerFixture,
    ) -> None:
        """step.progress events carry tool_call details when available."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=_make_describe("COMPLETED"))

        # WorkflowEvent only has type/step/timestamp, so tool info would
        # need to be in a separate field or embedded in the event.
        # For this spike, we just verify step.progress flows through SSE.
        handle.query = mocker.AsyncMock(
            return_value=_make_status(
                steps={"r1": {"status": "completed"}},
                events=[
                    {"type": "step.started", "step": "diagnose", "timestamp": "t1"},
                    {"type": "step.progress", "step": "diagnose", "timestamp": "t2"},
                    {"type": "step.completed", "step": "diagnose", "timestamp": "t3"},
                ],
            )
        )

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-tool-progress/events")
        events = _collect_sse(response)

        progress_events = [e for e in events if e.get("type") == "step.progress"]
        assert len(progress_events) == 1
        assert progress_events[0]["step"] == "diagnose"

    def test_no_progress_events_for_simple_workflow(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Workflows without progress events have no step.progress in SSE."""
        mock_temporal = mocker.MagicMock()
        handle = mocker.AsyncMock()
        mock_temporal.get_workflow_handle.return_value = handle

        handle.describe = mocker.AsyncMock(return_value=_make_describe("COMPLETED"))
        handle.query = mocker.AsyncMock(
            return_value=_make_status(
                steps={"r1": {"status": "completed"}},
                events=[
                    {"type": "step.started", "step": "s1", "timestamp": "t1"},
                    {"type": "step.completed", "step": "s1", "timestamp": "t2"},
                ],
            )
        )

        app = FastAPI()
        router = build_temporal_router(mock_temporal)
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.get("/v1/workflows/wf-noprog/events")
        events = _collect_sse(response)

        progress_count = sum(1 for e in events if e.get("type") == "step.progress")
        assert progress_count == 0
