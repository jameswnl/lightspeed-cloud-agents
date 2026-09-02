"""Tests for the shared native-structured-output schema guard (#235 follow-up).

`direct.py` (spawn: none) and `subprocess_child.py` (spawn: local) each had
their own copy of the object-root check that gates attempting pydantic-ai's
output_mode="native". This module is the single source of truth both import,
so the two executors can't drift on which schema shapes are eligible.
"""

from __future__ import annotations


class TestSupportsNativeOutput:
    def test_object_root_is_supported(self) -> None:
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert supports_native_output({"type": "object"}) is True

    def test_array_root_is_not_supported(self) -> None:
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert supports_native_output({"type": "array", "items": {"type": "string"}}) is False

    def test_missing_type_is_not_supported(self) -> None:
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert supports_native_output({"properties": {}}) is False

    def test_union_schema_is_not_supported(self) -> None:
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert supports_native_output({"anyOf": [{"type": "object"}, {"type": "null"}]}) is False


class TestDirectAndSubprocessChildShareTheGuard:
    """Both executors import the same function -- not independent copies."""

    def test_direct_py_imports_shared_helper(self) -> None:
        from cloud_agents.workflow.executor.step import direct
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert direct._supports_native_output is supports_native_output

    def test_subprocess_child_imports_shared_helper(self) -> None:
        from cloud_agents.workflow.executor.step import subprocess_child
        from cloud_agents.workflow.executor.step.native_output import supports_native_output

        assert subprocess_child._supports_native_output is supports_native_output
