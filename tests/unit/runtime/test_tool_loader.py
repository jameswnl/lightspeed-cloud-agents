"""Unit tests for tool loader."""

import pytest

from agents.definition import ToolsSpec
from agents.runtime.tool_loader import ToolLoadError, load_tools


class TestLoadTools:
    """Tests for load_tools function."""

    def test_loads_from_known_module(self) -> None:
        """Test loading tools from an existing module."""
        spec = ToolsSpec(module="examples.agents.diagnostic.tools", functions=["list_hosts", "check_host"])
        tools = load_tools(spec)
        assert len(tools) == 2
        assert tools[0][0] == "list_hosts"
        assert callable(tools[0][1])

    def test_missing_module_raises(self) -> None:
        """Test that a missing module raises ToolLoadError."""
        spec = ToolsSpec(module="nonexistent_module_xyz", functions=["fn"])
        with pytest.raises(ToolLoadError, match="Cannot import"):
            load_tools(spec)

    def test_missing_function_raises(self) -> None:
        """Test that a missing function raises ToolLoadError."""
        spec = ToolsSpec(module="examples.agents.diagnostic.tools", functions=["nonexistent_fn"])
        with pytest.raises(ToolLoadError, match="not found"):
            load_tools(spec)

    def test_non_callable_raises(self) -> None:
        """Test that a non-callable attribute raises ToolLoadError."""
        spec = ToolsSpec(module="examples.agents.diagnostic.cluster_state", functions=["cluster_state"])
        with pytest.raises(ToolLoadError, match="not callable"):
            load_tools(spec)

    def test_all_diagnostic_tools_load(self) -> None:
        """Test that all diagnostic tools load successfully."""
        spec = ToolsSpec(
            module="examples.agents.diagnostic.tools",
            functions=["list_hosts", "check_host", "get_alerts", "get_recent_deploys", "run_remediation"],
        )
        tools = load_tools(spec)
        assert len(tools) == 5
