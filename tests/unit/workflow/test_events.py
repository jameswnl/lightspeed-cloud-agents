"""Unit tests for workflow event model."""

from __future__ import annotations

import json

from agents.workflow.events import WorkflowEvent


class TestWorkflowEvent:
    """Tests for WorkflowEvent model."""

    def test_create_event(self) -> None:
        """Test basic event creation."""
        event = WorkflowEvent(
            event_type="workflow.started",
            workflow_id="wf-123",
        )
        assert event.event_type == "workflow.started"
        assert event.workflow_id == "wf-123"
        assert event.timestamp is not None

    def test_step_event(self) -> None:
        """Test step-level event with data."""
        event = WorkflowEvent(
            event_type="step.completed",
            workflow_id="wf-123",
            step_name="diagnose",
            data={"output_summary": "3 issues found"},
        )
        assert event.step_name == "diagnose"
        assert event.data["output_summary"] == "3 issues found"

    def test_to_sse_format(self) -> None:
        """Test SSE formatting."""
        event = WorkflowEvent(
            event_type="step.started",
            workflow_id="wf-123",
            step_name="diagnose",
        )
        sse = event.to_sse()
        assert sse.startswith("event: step.started\n")
        assert "data: " in sse
        assert sse.endswith("\n\n")
        data_line = sse.split("data: ")[1].strip()
        parsed = json.loads(data_line)
        assert parsed["event_type"] == "step.started"
        assert parsed["workflow_id"] == "wf-123"

    def test_to_sse_excludes_none(self) -> None:
        """Test that None fields are excluded from SSE output."""
        event = WorkflowEvent(
            event_type="workflow.completed",
            workflow_id="wf-123",
        )
        sse = event.to_sse()
        data_line = sse.split("data: ")[1].strip()
        parsed = json.loads(data_line)
        assert "step_name" not in parsed
        assert "data" not in parsed

    def test_all_event_types(self) -> None:
        """Test all event types are valid."""
        types = [
            "workflow.started", "step.started", "step.completed",
            "step.failed", "step.skipped", "workflow.paused",
            "workflow.completed", "workflow.failed",
        ]
        for t in types:
            event = WorkflowEvent(event_type=t, workflow_id="wf-1")
            assert event.event_type == t
