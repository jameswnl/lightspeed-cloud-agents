"""WorkflowDefinition model — Pydantic schema for workflow.yaml.

Defines the YAML contract for multi-step agent workflows.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class WorkflowStepSpec(BaseModel):
    """A single step in a workflow.

    Attributes:
        name: Unique step identifier within the workflow.
        type: Step type — agent (calls an agent) or human-approval (pauses for approval).
        agent: Agent name for type=agent (resolved via AgentRegistry).
        prompt: Prompt template for the agent, supports {{ steps.X.output.Y }}.
        output_key: Key in workflow state for this step's output.
        condition: Optional expression — skip step if evaluates to false.
        message: Human-readable message for type=human-approval.
        timeout_seconds: Maximum seconds for this step.
    """

    name: str
    type: Literal["agent", "human-approval"]
    agent: Optional[str] = None
    prompt: Optional[str] = None
    output_key: str
    condition: Optional[str] = None
    message: Optional[str] = None
    timeout_seconds: int = 3600
    max_retries: int = Field(default=1, ge=1)


class WorkflowSpec(BaseModel):
    """Full workflow specification.

    Attributes:
        input_prompt: Optional initial prompt passed to the first step.
        steps: Ordered list of workflow steps.
    """

    input_prompt: Optional[str] = None
    steps: list[WorkflowStepSpec] = Field(..., min_length=1)


class WorkflowDefinition(BaseModel):
    """Top-level workflow definition from workflow.yaml.

    Attributes:
        apiVersion: API version string.
        kind: Must be AgentWorkflow.
        metadata: Workflow metadata including name.
        spec: Full workflow specification.
    """

    apiVersion: str
    kind: Literal["AgentWorkflow"]
    metadata: dict[str, Any]
    spec: WorkflowSpec
