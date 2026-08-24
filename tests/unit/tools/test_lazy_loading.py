"""Tests for lazy loading of built-in tools.

Verifies that importing cloud_agents.tools no longer auto-registers
builtins and that load_builtin_tools() must be called explicitly.
"""

from __future__ import annotations

import sys

import pytest

from cloud_agents.workflow.executor.step.tools import clear_tools, list_tools

# Tool modules whose @step_tool decorators fire on first import.
_TOOL_MODULES = [
    "cloud_agents.tools",
    "cloud_agents.tools.kubectl",
    "cloud_agents.tools.http_request",
    "cloud_agents.tools.read_file",
]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear tool registry and module cache before/after each test."""
    clear_tools()
    for mod in _TOOL_MODULES:
        sys.modules.pop(mod, None)
    yield
    clear_tools()
    for mod in _TOOL_MODULES:
        sys.modules.pop(mod, None)


class TestLazyLoading:
    """Tests that builtin tools are NOT loaded on import."""

    def test_import_does_not_register_builtins(self) -> None:
        """Importing cloud_agents.tools does NOT register built-in tools."""
        import cloud_agents.tools  # noqa: F401

        registered = list_tools()
        assert "kubectl_get" not in registered
        assert "http_request" not in registered
        assert "read_file" not in registered

    def test_load_builtin_tools_registers_all(self) -> None:
        """load_builtin_tools() registers all 3 built-in tools."""
        from cloud_agents.tools import load_builtin_tools

        load_builtin_tools()
        registered = list_tools()
        assert "kubectl_get" in registered
        assert "http_request" in registered
        assert "read_file" in registered

    def test_load_builtin_tools_idempotent(self) -> None:
        """Calling load_builtin_tools() twice does not raise or double-register."""
        from cloud_agents.tools import load_builtin_tools

        load_builtin_tools()
        first_tools = list_tools()

        # Second call should be a no-op (not raise ValueError from re-registration)
        load_builtin_tools()
        second_tools = list_tools()

        assert first_tools == second_tools

    def test_load_builtin_tools_exported_in_all(self) -> None:
        """load_builtin_tools is in __all__."""
        import cloud_agents.tools

        assert "load_builtin_tools" in cloud_agents.tools.__all__
