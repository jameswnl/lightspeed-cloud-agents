"""Tools for the workflow designer agent.

Provides agent discovery, workflow validation, and feature listing
to help the designer agent generate valid workflow definitions.
"""

from __future__ import annotations

from typing import Any

import yaml

from agents.registry import AgentRegistry
from agents.workflow.definition import WorkflowDefinition


def create_designer_tools(registry: AgentRegistry) -> dict[str, Any]:
    """Create tool functions for the designer agent.

    Args:
        registry: Agent registry for discovering available agents.

    Returns:
        Dict of tool name → callable.
    """

    def list_available_agents() -> str:
        """List all registered agents and their endpoints."""
        agent_names = registry.list_agents()
        if not agent_names:
            return "No agents registered."
        lines = [f"- {name}: {registry.get_endpoint(name)}" for name in agent_names]
        return "Available agents:\n" + "\n".join(lines)

    def validate_workflow(yaml_str: str) -> str:
        """Validate a workflow definition YAML string.

        Args:
            yaml_str: The workflow YAML to validate.

        Returns:
            Validation result — either "valid" or error details.
        """
        try:
            data = yaml.safe_load(yaml_str)
            WorkflowDefinition.model_validate(data)
            return "valid"
        except Exception as exc:
            return f"invalid: {exc}"

    def list_workflow_features() -> str:
        """List available workflow features and syntax."""
        return (
            "Workflow features:\n"
            "- Step types: agent, human-approval\n"
            "- Conditions: steps.X.output.Y == value (and/or supported)\n"
            "- Interpolation: {{ steps.X.output.Y }} or nested {{ steps.X.output.a.b[0].c }}\n"
            "- Retry: max_retries (default 1)\n"
            "- Spawning: spawn: pre-deployed (default) or on-demand\n"
            "- Parallel: parallel_group for concurrent steps\n"
            "- Permissions: per-step allowed_tools/denied_tools/service_account\n"
            "- Advisory mode: metadata.mode: advisory for read-only execution\n"
        )

    return {
        "list_available_agents": list_available_agents,
        "validate_workflow": validate_workflow,
        "list_workflow_features": list_workflow_features,
    }
