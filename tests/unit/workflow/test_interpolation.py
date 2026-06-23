"""Unit tests for prompt template interpolation."""

import pytest

from agents.workflow.interpolation import interpolate
from agents.workflow.state import StepResult, WorkflowState


def _make_state(**step_outputs: dict) -> WorkflowState:
    """Create a WorkflowState with step outputs."""
    steps = {}
    for name, output in step_outputs.items():
        steps[name] = StepResult(step_name=name, status="completed", output=output)
    return WorkflowState(
        workflow_id="w1", workflow_name="test",
        created_at="2026-01-01", updated_at="2026-01-01",
        steps=steps,
    )


class TestInterpolate:
    """Tests for interpolate function."""

    def test_simple_string_value(self) -> None:
        """Test interpolating a string value."""
        state = _make_state(diagnose={"summary": "All good"})
        result = interpolate("Result: {{ steps.diagnose.output.summary }}", state)
        assert result == "Result: <data>All good</data>"

    def test_list_value_json_serialized(self) -> None:
        """Test that list values are JSON-serialized."""
        state = _make_state(diagnose={"issues": ["a", "b"]})
        result = interpolate("Issues: {{ steps.diagnose.output.issues }}", state)
        assert '"a"' in result
        assert "<data>" in result

    def test_dict_value_json_serialized(self) -> None:
        """Test that dict values are JSON-serialized."""
        state = _make_state(diagnose={"detail": {"host": "web-02", "cpu": 92}})
        result = interpolate("Detail: {{ steps.diagnose.output.detail }}", state)
        assert '"host"' in result
        assert "<data>" in result

    def test_none_value(self) -> None:
        """Test that None is rendered as null."""
        state = _make_state(diagnose={"missing": None})
        result = interpolate("Val: {{ steps.diagnose.output.missing }}", state)
        assert result == "Val: <data>null</data>"

    def test_missing_step_raises(self) -> None:
        """Test that referencing a missing step raises ValueError."""
        state = _make_state()
        with pytest.raises(ValueError, match="missing step"):
            interpolate("{{ steps.nonexistent.output.x }}", state)

    def test_missing_key_in_output(self) -> None:
        """Test that a missing key in output returns None → null."""
        state = _make_state(diagnose={"summary": "ok"})
        result = interpolate("{{ steps.diagnose.output.missing_key }}", state)
        assert result == "<data>null</data>"

    def test_no_templates_passthrough(self) -> None:
        """Test that text without templates passes through unchanged."""
        state = _make_state()
        result = interpolate("No templates here", state)
        assert result == "No templates here"

    def test_multiple_templates(self) -> None:
        """Test multiple templates in one string."""
        state = _make_state(
            s1={"a": "hello"},
            s2={"b": "world"},
        )
        result = interpolate(
            "{{ steps.s1.output.a }} {{ steps.s2.output.b }}", state
        )
        assert "<data>hello</data>" in result
        assert "<data>world</data>" in result

    def test_boolean_value(self) -> None:
        """Test boolean value interpolation."""
        state = _make_state(check={"healthy": True})
        result = interpolate("{{ steps.check.output.healthy }}", state)
        assert result == "<data>true</data>"
