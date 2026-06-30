"""Unit tests for output type registry."""

import pytest

from cloud_agents.models import DiagnosticReport, MonitoringResult
from cloud_agents.runtime.output_types import resolve_output_type


class TestResolveOutputType:
    """Tests for resolve_output_type."""

    def test_builtin_diagnostic_report(self) -> None:
        """Test resolving DiagnosticReport from built-in registry."""
        assert resolve_output_type("DiagnosticReport") is DiagnosticReport

    def test_builtin_monitoring_result(self) -> None:
        """Test resolving MonitoringResult from built-in registry."""
        assert resolve_output_type("MonitoringResult") is MonitoringResult

    def test_builtin_str(self) -> None:
        """Test resolving str from built-in registry."""
        assert resolve_output_type("str") is str

    def test_unknown_without_module_raises(self) -> None:
        """Test that unknown type without module raises ValueError."""
        with pytest.raises(ValueError, match="Unknown output_type"):
            resolve_output_type("NonexistentType")

    def test_importlib_fallback_from_module(self) -> None:
        """Test loading custom type from a module via importlib."""
        result = resolve_output_type(
            "DiagnosticReport",
            module_name="cloud_agents.models",
        )
        assert result is DiagnosticReport

    def test_importlib_fallback_unknown_class_raises(self) -> None:
        """Test that unknown class in a valid module raises ValueError."""
        with pytest.raises(ValueError, match="Unknown output_type"):
            resolve_output_type("NonexistentClass", module_name="cloud_agents.models")

    def test_importlib_fallback_bad_module_raises(self) -> None:
        """Test that invalid module raises ValueError."""
        with pytest.raises(ValueError, match="Unknown output_type"):
            resolve_output_type("SomeType", module_name="nonexistent_module")
