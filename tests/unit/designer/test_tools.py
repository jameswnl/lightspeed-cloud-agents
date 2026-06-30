"""Unit tests for workflow designer tools."""

from __future__ import annotations

from cloud_agents.designer.tools import create_designer_tools
from cloud_agents.registry import AgentRegistry


def _mock_registry() -> AgentRegistry:
    """Create a test registry."""
    return AgentRegistry(
        {
            "diagnostic-agent": "http://diag:8080",
            "monitoring-agent": "http://mon:8080",
        }
    )


class TestDesignerTools:
    """Tests for designer agent tools."""

    def test_list_available_agents(self) -> None:
        """Test listing available agents."""
        tools = create_designer_tools(_mock_registry())
        result = tools["list_available_agents"]()
        assert "diagnostic-agent" in result
        assert "monitoring-agent" in result

    def test_list_available_agents_empty(self) -> None:
        """Test listing with no agents."""
        tools = create_designer_tools(AgentRegistry({}))
        result = tools["list_available_agents"]()
        assert "No agents" in result

    def test_validate_workflow_valid(self) -> None:
        """Test validating a valid workflow YAML."""
        tools = create_designer_tools(_mock_registry())
        yaml_str = """
apiVersion: v1
kind: AgentWorkflow
metadata:
  name: test-workflow
spec:
  steps:
    - name: diagnose
      type: agent
      agent: diagnostic-agent
      prompt: Check all hosts
      output_key: diagnosis
"""
        result = tools["validate_workflow"](yaml_str)
        assert result == "valid"

    def test_validate_workflow_invalid(self) -> None:
        """Test validating an invalid workflow YAML."""
        tools = create_designer_tools(_mock_registry())
        result = tools["validate_workflow"]("not valid yaml: [")
        assert "invalid" in result

    def test_validate_workflow_missing_fields(self) -> None:
        """Test validating YAML with missing required fields."""
        tools = create_designer_tools(_mock_registry())
        result = tools["validate_workflow"]("apiVersion: v1\nkind: AgentWorkflow\n")
        assert "invalid" in result

    def test_list_workflow_features(self) -> None:
        """Test listing workflow features."""
        tools = create_designer_tools(_mock_registry())
        result = tools["list_workflow_features"]()
        assert "agent" in result
        assert "human-approval" in result
        assert "parallel_group" in result
        assert "advisory" in result
