"""Tests for StreamEvent dataclass."""

from __future__ import annotations


class TestStreamEventConstruction:
    """Tests for StreamEvent construction with defaults."""

    def test_token_event_defaults(self) -> None:
        """StreamEvent with type='token' has default empty data and no result."""
        from cloud_agents.workflow.executor.step.base import StreamEvent

        event = StreamEvent(type="token")
        assert event.type == "token"
        assert event.data == {}
        assert event.result is None

    def test_token_event_with_data(self) -> None:
        """StreamEvent carries delta payload in data."""
        from cloud_agents.workflow.executor.step.base import StreamEvent

        event = StreamEvent(type="token", data={"delta": "Hello"})
        assert event.data == {"delta": "Hello"}

    def test_complete_event_with_result(self) -> None:
        """StreamEvent type='complete' carries a StepResult."""
        from cloud_agents.workflow.executor.step.base import StepResult, StreamEvent

        result = StepResult(
            status="completed",
            output={"response": "done"},
            input_tokens=10,
            output_tokens=5,
            duration_ms=100,
        )
        event = StreamEvent(type="complete", result=result)
        assert event.type == "complete"
        assert event.result is result
        assert event.result.status == "completed"

    def test_error_event(self) -> None:
        """StreamEvent type='error' carries error data and a failed StepResult."""
        from cloud_agents.workflow.executor.step.base import StepResult, StreamEvent

        result = StepResult(status="failed", error="boom", duration_ms=50)
        event = StreamEvent(
            type="error",
            data={"error": "boom"},
            result=result,
        )
        assert event.type == "error"
        assert event.data["error"] == "boom"
        assert event.result.status == "failed"

    def test_all_event_types(self) -> None:
        """All three event types can be constructed."""
        from cloud_agents.workflow.executor.step.base import StreamEvent

        types = ["token", "complete", "error"]
        for t in types:
            event = StreamEvent(type=t)
            assert event.type == t
