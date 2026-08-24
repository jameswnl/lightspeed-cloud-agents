"""Tests for tool package exports and re-exports."""

from __future__ import annotations


class TestToolPackageExports:
    """Tests that cloud_agents.tools re-exports the public API."""

    def test_list_tools_reexported(self) -> None:
        """list_tools is accessible from cloud_agents.tools."""
        from cloud_agents.tools import list_tools

        # Should be the same function as the one in the registry module
        from cloud_agents.workflow.executor.step.tools import (
            list_tools as original_list_tools,
        )

        assert list_tools is original_list_tools

    def test_step_tool_reexported(self) -> None:
        """step_tool is accessible from cloud_agents.tools."""
        from cloud_agents.tools import step_tool

        from cloud_agents.workflow.executor.step.tools import (
            step_tool as original_step_tool,
        )

        assert step_tool is original_step_tool

    def test_register_tool_reexported(self) -> None:
        """register_tool is accessible from cloud_agents.tools."""
        from cloud_agents.tools import register_tool

        from cloud_agents.workflow.executor.step.tools import (
            register_tool as original_register_tool,
        )

        assert register_tool is original_register_tool

    def test_clear_tools_reexported(self) -> None:
        """clear_tools is accessible from cloud_agents.tools."""
        from cloud_agents.tools import clear_tools

        from cloud_agents.workflow.executor.step.tools import (
            clear_tools as original_clear_tools,
        )

        assert clear_tools is original_clear_tools

    def test_load_builtin_tools_reexported(self) -> None:
        """load_builtin_tools is accessible from cloud_agents.tools."""
        from cloud_agents.tools import load_builtin_tools

        assert callable(load_builtin_tools)
