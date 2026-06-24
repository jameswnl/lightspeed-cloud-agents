"""Unit tests for prompt template interpolation."""

import pytest

from agents.workflow.interpolation import interpolate, resolve_path
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


class TestResolvePath:
    """Tests for resolve_path helper."""

    def test_single_key(self) -> None:
        """Test resolving a single key."""
        assert resolve_path({"host": "web-02"}, "host") == "web-02"

    def test_nested_keys(self) -> None:
        """Test resolving nested dot-separated keys."""
        data = {"details": {"host": "web-02", "cpu": 92}}
        assert resolve_path(data, "details.host") == "web-02"
        assert resolve_path(data, "details.cpu") == 92

    def test_array_index(self) -> None:
        """Test resolving array index."""
        data = {"items": ["a", "b", "c"]}
        assert resolve_path(data, "items[0]") == "a"
        assert resolve_path(data, "items[2]") == "c"

    def test_nested_array_access(self) -> None:
        """Test resolving nested object inside array."""
        data = {"actions": [{"host": "web-01", "result": "ok"}, {"host": "web-02", "result": "fail"}]}
        assert resolve_path(data, "actions[0].host") == "web-01"
        assert resolve_path(data, "actions[1].result") == "fail"

    def test_deeply_nested(self) -> None:
        """Test resolving deeply nested paths."""
        data = {"a": {"b": {"c": {"d": "deep"}}}}
        assert resolve_path(data, "a.b.c.d") == "deep"

    def test_missing_key_raises(self) -> None:
        """Test that missing key raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            resolve_path({"host": "web-02"}, "missing")

    def test_missing_nested_key_raises(self) -> None:
        """Test that missing nested key raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            resolve_path({"details": {"host": "web-02"}}, "details.missing")

    def test_array_index_out_of_bounds_raises(self) -> None:
        """Test that out of bounds array index raises ValueError."""
        with pytest.raises(ValueError, match="out of range"):
            resolve_path({"items": ["a", "b"]}, "items[5]")

    def test_array_index_on_non_list_raises(self) -> None:
        """Test that array index on non-list raises ValueError."""
        with pytest.raises(ValueError, match="not a list"):
            resolve_path({"host": "web-02"}, "host[0]")

    def test_key_on_non_dict_raises(self) -> None:
        """Test that key access on non-dict raises ValueError."""
        with pytest.raises(ValueError, match="not a dict"):
            resolve_path({"host": "web-02"}, "host.sub")


class TestNestedInterpolation:
    """Tests for nested path interpolation through interpolate()."""

    def test_nested_dict_access(self) -> None:
        """Test interpolating nested dict values."""
        state = _make_state(diagnose={"details": {"host": "web-02", "cpu": 92}})
        result = interpolate("Host: {{ steps.diagnose.output.details.host }}", state)
        assert result == "Host: <data>web-02</data>"

    def test_array_index_access(self) -> None:
        """Test interpolating array element."""
        state = _make_state(diagnose={"issues": ["disk full", "high cpu"]})
        result = interpolate("First: {{ steps.diagnose.output.issues[0] }}", state)
        assert result == "First: <data>disk full</data>"

    def test_array_object_access(self) -> None:
        """Test interpolating field from array element."""
        state = _make_state(diagnose={
            "actions": [
                {"host": "web-01", "action": "restart"},
                {"host": "web-02", "action": "rollback"},
            ]
        })
        result = interpolate("{{ steps.diagnose.output.actions[1].host }}", state)
        assert result == "<data>web-02</data>"

    def test_backward_compat_simple_key(self) -> None:
        """Test that simple single-key access still works."""
        state = _make_state(diagnose={"summary": "All good"})
        result = interpolate("{{ steps.diagnose.output.summary }}", state)
        assert result == "<data>All good</data>"

    def test_nested_returns_dict_as_json(self) -> None:
        """Test that nested path returning a dict is JSON-serialized."""
        state = _make_state(diagnose={"details": {"host": "web-02", "cpu": 92}})
        result = interpolate("{{ steps.diagnose.output.details }}", state)
        assert "<data>" in result
        assert '"host"' in result

    def test_nested_missing_intermediate_returns_null(self) -> None:
        """Test that missing intermediate key returns null."""
        state = _make_state(diagnose={"summary": "ok"})
        result = interpolate("{{ steps.diagnose.output.details.host }}", state)
        assert result == "<data>null</data>"
