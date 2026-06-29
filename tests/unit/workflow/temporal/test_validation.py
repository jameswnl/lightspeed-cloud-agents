"""Unit tests for workflow definition validation."""

from __future__ import annotations

from agents.workflow.temporal_validation import validate_definition


class TestDefinitionValidation:
    """Tests for validate_definition."""

    def test_valid_definition_passes(self) -> None:
        """Valid definition raises no errors."""
        defn = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "output_key": "r1",
                 "prompt": "check", "spawn": "ephemeral"},
            ]},
        }
        errors = validate_definition(defn)
        assert len(errors) == 0

    def test_duplicate_output_key(self) -> None:
        """Duplicate output_key is caught."""
        defn = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "output_key": "r1", "prompt": "a"},
                {"name": "s2", "type": "agent", "output_key": "r1", "prompt": "b"},
            ]},
        }
        errors = validate_definition(defn)
        assert any("duplicate" in e.lower() for e in errors)

    def test_undefined_step_reference(self) -> None:
        """Reference to undefined step in prompt template is caught."""
        defn = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "output_key": "r1",
                 "prompt": "fix {{ steps.nonexistent.output.summary }}"},
            ]},
        }
        errors = validate_definition(defn)
        assert any("nonexistent" in e for e in errors)

    def test_missing_name(self) -> None:
        """Step without name is caught."""
        defn = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"type": "agent", "output_key": "r1", "prompt": "check"},
            ]},
        }
        errors = validate_definition(defn)
        assert any("name" in e.lower() for e in errors)
