"""Unit tests for workflow definition validation."""

from __future__ import annotations

from cloud_agents.workflow.temporal_validation import validate_definition


class TestDefinitionValidation:
    """Tests for validate_definition."""

    def test_valid_definition_passes(self) -> None:
        """Valid definition raises no errors."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "output_key": "r1",
                        "prompt": "check",
                        "spawn": "ephemeral",
                    },
                ]
            },
        }
        errors = validate_definition(defn)
        assert len(errors) == 0

    def test_duplicate_output_key(self) -> None:
        """Duplicate output_key is caught."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "s1", "type": "agent", "output_key": "r1", "prompt": "a"},
                    {"name": "s2", "type": "agent", "output_key": "r1", "prompt": "b"},
                ]
            },
        }
        errors = validate_definition(defn)
        assert any("duplicate" in e.lower() for e in errors)

    def test_undefined_step_reference(self) -> None:
        """Reference to undefined step in prompt template is caught."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "output_key": "r1",
                        "prompt": "fix {{ steps.nonexistent.output.summary }}",
                    },
                ]
            },
        }
        errors = validate_definition(defn)
        assert any("nonexistent" in e for e in errors)

    def test_null_prompt_no_crash(self) -> None:
        """Step with prompt: null does not crash validation."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {
                        "name": "approve",
                        "type": "human-approval",
                        "output_key": "a1",
                        "prompt": None,
                        "message": "Approve?",
                    },
                ]
            },
        }
        errors = validate_definition(defn)
        assert len(errors) == 0

    def test_missing_prompt_no_crash(self) -> None:
        """Step without prompt key does not crash validation."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {
                        "name": "approve",
                        "type": "human-approval",
                        "output_key": "a1",
                        "message": "Approve?",
                    },
                ]
            },
        }
        errors = validate_definition(defn)
        assert len(errors) == 0

    def test_missing_name(self) -> None:
        """Step without name is caught."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"type": "agent", "output_key": "r1", "prompt": "check"},
                ]
            },
        }
        errors = validate_definition(defn)
        assert any("name" in e.lower() for e in errors)


class TestOutputSchemaValidation:
    """Tests for output_schema validation in workflow definitions."""

    def _defn_with_schema(self, schema: dict) -> dict:
        """Helper to build a definition with a given output_schema."""
        return {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {
                        "name": "s1",
                        "type": "agent",
                        "output_key": "r1",
                        "prompt": "check",
                        "output_schema": schema,
                    },
                ]
            },
        }

    def test_array_without_items_rejected(self) -> None:
        """Array type without items definition is rejected."""
        errors = validate_definition(self._defn_with_schema(
            {"type": "object", "properties": {"things": {"type": "array"}}}
        ))
        assert any("items" in e.lower() and "things" in e for e in errors)

    def test_array_with_items_passes(self) -> None:
        """Array type with items definition passes."""
        errors = validate_definition(self._defn_with_schema(
            {"type": "object", "properties": {"things": {"type": "array", "items": {"type": "string"}}}}
        ))
        assert not any("items" in e.lower() for e in errors)

    def test_nested_array_without_items_rejected(self) -> None:
        """Nested array without items is caught recursively."""
        errors = validate_definition(self._defn_with_schema({
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner_list": {"type": "array"}
                    }
                }
            }
        }))
        assert any("items" in e.lower() and "root.outer.inner_list" in e for e in errors)

    def test_no_output_schema_passes(self) -> None:
        """Step without output_schema passes validation."""
        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {
                "steps": [
                    {"name": "s1", "type": "agent", "output_key": "r1", "prompt": "check"},
                ]
            },
        }
        errors = validate_definition(defn)
        assert not any("output_schema" in e.lower() for e in errors)

    def test_valid_complex_schema_passes(self) -> None:
        """Complex schema with arrays, objects, and nesting passes."""
        errors = validate_definition(self._defn_with_schema({
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "string"}},
                "details": {
                    "type": "object",
                    "properties": {
                        "hosts": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
                    }
                }
            },
            "required": ["summary"]
        }))
        assert len(errors) == 0

    def test_multiple_arrays_without_items_all_reported(self) -> None:
        """Multiple arrays without items each produce an error."""
        errors = validate_definition(self._defn_with_schema({
            "type": "object",
            "properties": {
                "list_a": {"type": "array"},
                "list_b": {"type": "array"},
            }
        }))
        items_errors = [e for e in errors if "items" in e.lower()]
        assert len(items_errors) == 2

    def test_error_message_includes_step_name(self) -> None:
        """Error message includes the step name for context."""
        errors = validate_definition(self._defn_with_schema(
            {"type": "object", "properties": {"things": {"type": "array"}}}
        ))
        assert any("s1" in e for e in errors)
