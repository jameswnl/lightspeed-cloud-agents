"""Tests for the step tool registry."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Clear tool registry before each test to avoid cross-test pollution."""
    from cloud_agents.workflow.executor.step.tools import clear_tools

    clear_tools()
    yield  # type: ignore[misc]
    clear_tools()


def _sample_tool(query: str) -> str:
    """A sample tool function for testing.

    Parameters:
        query: Input query.

    Returns:
        Fixed string.
    """
    return f"result: {query}"


def _another_tool(name: str) -> str:
    """Another sample tool for testing.

    Parameters:
        name: Input name.

    Returns:
        Fixed string.
    """
    return f"hello {name}"


class TestRegisterTool:
    """Tests for register_tool()."""

    def test_register_tool_adds_to_registry(self) -> None:
        """register_tool() makes the tool available via get_tools()."""
        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("kubectl_get", _sample_tool)
        tools = get_tools(["kubectl_get"])
        assert len(tools) == 1

    def test_register_tool_with_description(self) -> None:
        """register_tool() accepts optional description."""
        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("kubectl_get", _sample_tool, description="Get K8s resources")
        tools = get_tools(["kubectl_get"])
        assert len(tools) == 1

    def test_duplicate_registration_raises(self) -> None:
        """register_tool() raises ValueError on duplicate name."""
        from cloud_agents.workflow.executor.step.tools import register_tool

        register_tool("kubectl_get", _sample_tool)
        with pytest.raises(ValueError, match="already registered"):
            register_tool("kubectl_get", _another_tool)


class TestGetTools:
    """Tests for get_tools()."""

    def test_returns_tools_for_known_names(self) -> None:
        """get_tools() returns Tool instances for registered names."""
        from pydantic_ai import Tool

        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("kubectl_get", _sample_tool)
        register_tool("kubectl_describe", _another_tool)
        tools = get_tools(["kubectl_get", "kubectl_describe"])
        assert len(tools) == 2
        assert all(isinstance(t, Tool) for t in tools)

    def test_raises_for_unknown_name(self) -> None:
        """get_tools() raises ValueError for unknown tool names."""
        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("kubectl_get", _sample_tool)
        with pytest.raises(ValueError, match="Unknown tool 'nonexistent'"):
            get_tools(["nonexistent"])

    def test_raises_shows_available_tools(self) -> None:
        """get_tools() error message lists available tools."""
        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("kubectl_get", _sample_tool)
        with pytest.raises(ValueError, match="kubectl_get"):
            get_tools(["nonexistent"])

    def test_empty_list_returns_empty(self) -> None:
        """get_tools([]) returns empty list."""
        from cloud_agents.workflow.executor.step.tools import get_tools

        assert get_tools([]) == []

    def test_preserves_order(self) -> None:
        """get_tools() returns tools in the requested order."""
        from cloud_agents.workflow.executor.step.tools import get_tools, register_tool

        register_tool("b_tool", _another_tool)
        register_tool("a_tool", _sample_tool)
        tools = get_tools(["b_tool", "a_tool"])
        assert tools[0].name == "b_tool"
        assert tools[1].name == "a_tool"


class TestListTools:
    """Tests for list_tools()."""

    def test_empty_registry(self) -> None:
        """list_tools() returns empty list when nothing registered."""
        from cloud_agents.workflow.executor.step.tools import list_tools

        assert list_tools() == []

    def test_returns_sorted_names(self) -> None:
        """list_tools() returns names in sorted order."""
        from cloud_agents.workflow.executor.step.tools import list_tools, register_tool

        register_tool("z_tool", _sample_tool)
        register_tool("a_tool", _another_tool)
        assert list_tools() == ["a_tool", "z_tool"]


class TestStepToolDecorator:
    """Tests for @step_tool decorator."""

    def test_registers_decorated_function(self) -> None:
        """@step_tool registers the function under the given name."""
        from cloud_agents.workflow.executor.step.tools import list_tools, step_tool

        @step_tool("my_tool")
        def my_func(x: str) -> str:
            """A tool."""
            return x

        assert "my_tool" in list_tools()

    def test_returns_original_function(self) -> None:
        """@step_tool returns the unmodified function."""
        from cloud_agents.workflow.executor.step.tools import step_tool

        @step_tool("my_tool")
        def my_func(x: str) -> str:
            """A tool."""
            return x

        assert my_func("hello") == "hello"

    def test_decorator_with_description(self) -> None:
        """@step_tool accepts description kwarg."""
        from cloud_agents.workflow.executor.step.tools import get_tools, step_tool

        @step_tool("my_tool", description="Does things")
        def my_func(x: str) -> str:
            """A tool."""
            return x

        tools = get_tools(["my_tool"])
        assert len(tools) == 1


class TestClearTools:
    """Tests for clear_tools()."""

    def test_removes_all_tools(self) -> None:
        """clear_tools() removes all registered tools."""
        from cloud_agents.workflow.executor.step.tools import (
            clear_tools,
            list_tools,
            register_tool,
        )

        register_tool("tool_a", _sample_tool)
        register_tool("tool_b", _another_tool)
        assert len(list_tools()) == 2
        clear_tools()
        assert list_tools() == []
