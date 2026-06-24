"""Output models for the workflow designer agent."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class WorkflowDesign(BaseModel):
    """Output of the workflow designer agent.

    Attributes:
        workflow_yaml: The generated workflow definition as YAML string.
        rationale: Explanation of design decisions.
        validation_status: Whether the YAML passed schema validation.
        validation_errors: Any validation errors found.
    """

    workflow_yaml: str
    rationale: str
    validation_status: Literal["valid", "invalid"]
    validation_errors: list[str] = []


class AgentCapability(BaseModel):
    """Describes an available agent's capabilities.

    Attributes:
        name: Agent name from the registry.
        endpoint: Agent HTTP endpoint.
        tools: List of tool names (if known).
        output_type: Agent's output type (if known).
    """

    name: str
    endpoint: str
    tools: list[str] = []
    output_type: Optional[str] = None
