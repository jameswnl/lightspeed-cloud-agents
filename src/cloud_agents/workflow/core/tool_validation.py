"""Shared tool validation for workflow submission.

Validates that all tool names referenced in workflow steps are registered
in the tool registry. Used by both local and Temporal API routers.
"""

from __future__ import annotations

from typing import Any


def validate_workflow_tools(definition: dict[str, Any]) -> list[str]:
    """Validate tool names in a workflow definition against the registry.

    Parameters:
        definition: Workflow definition dict.

    Returns:
        List of error messages. Empty if all tools are valid.
    """
    from cloud_agents.workflow.executor.step.tools import list_tools

    registered = set(list_tools())
    errors: list[str] = []

    steps = definition.get("spec", {})
    if not isinstance(steps, dict):
        return errors
    step_list = steps.get("steps", [])
    if not isinstance(step_list, list):
        return errors

    for step in step_list:
        if not isinstance(step, dict):
            continue
        step_tools = step.get("tools", [])
        if not isinstance(step_tools, list):
            continue
        if step_tools:
            unknown = [t for t in step_tools if t not in registered]
            if unknown:
                errors.append(
                    f"Unknown tools in step '{step.get('name', '?')}': {unknown}. "
                    f"Registered: {sorted(registered) or '(none)'}"
                )

    return errors
