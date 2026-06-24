"""Workflow state models for tracking multi-step execution."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class StepResult(BaseModel):
    """Result of a single workflow step.

    Attributes:
        step_name: Identifier of the step.
        status: Current step status.
        output: Step output data (agent response output or approval result).
        error: Error message if the step failed.
        started_at: ISO timestamp when the step started.
        completed_at: ISO timestamp when the step completed.
    """

    step_name: str
    status: Literal[
        "pending", "running", "completed", "failed", "skipped", "awaiting_approval"
    ] = "pending"
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class WorkflowState(BaseModel):
    """Full state of a workflow execution.

    Attributes:
        workflow_id: Unique identifier for this execution.
        workflow_name: Name from the workflow definition.
        status: Overall workflow status.
        current_step: Name of the step currently executing.
        steps: Results keyed by step name.
        created_at: ISO timestamp when the workflow started.
        updated_at: ISO timestamp of the last state change.
    """

    workflow_id: str
    workflow_name: str
    status: Literal["running", "completed", "failed", "paused"] = "running"
    current_step: Optional[str] = None
    paused_step_index: Optional[int] = None
    steps: dict[str, StepResult] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    version: int = 1
