"""Shared data models for cloud agent HTTP communication.

These models define the contract between the core pod and agent pods.
AgentRunRequest/AgentRunResponse are the HTTP request/response bodies
for the agent runtime's /v1/run endpoint.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class AgentRunRequest(BaseModel):
    """Request body for POST /v1/run on an agent pod.

    Attributes:
        prompt: The task or question for the agent to process.
        context: Optional metadata such as correlation_id or trace_id.
    """

    prompt: str
    context: Optional[dict[str, Any]] = None

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_empty(cls, v: str) -> str:
        """Validate that prompt is not empty or whitespace-only."""
        if not v.strip():
            raise ValueError("prompt must not be empty")
        return v


class RemediationAction(BaseModel):
    """A single remediation action taken by an agent.

    Attributes:
        host: Target hostname where the action was performed.
        action: The action identifier (e.g. ``rollback_deploy:frontend``).
        result: Human-readable description of the outcome.
        success: Whether the action completed successfully.
    """

    host: str
    action: str
    result: str
    success: bool


class RollbackPlan(BaseModel):
    """Rollback plan for a remediation action.

    Attributes:
        description: What to do if the remediation fails.
        steps: Ordered rollback steps.
    """

    description: str
    steps: list[str] = Field(default_factory=list)


class DiagnosticReport(BaseModel):
    """Structured output from the diagnostic agent.

    Attributes:
        summary: Brief description of what was found and done.
        confidence: Confidence level in the diagnosis.
        risk_level: Risk level of the proposed/taken actions.
        issues_found: List of issues discovered during diagnosis.
        actions_taken: Remediation actions attempted.
        remaining_issues: Issues that could not be resolved.
        required_permissions: Permissions needed for the remediation.
        rollback_plan: What to do if remediation fails.
        cluster_healthy: Whether all hosts are healthy after remediation.
    """

    summary: str
    confidence: Literal["low", "medium", "high"] = "medium"
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    issues_found: list[str]
    actions_taken: list[RemediationAction]
    remaining_issues: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    rollback_plan: Optional[RollbackPlan] = None
    cluster_healthy: bool


class MonitoringAlert(BaseModel):
    """A single alert from the monitoring agent.

    Attributes:
        host: The host where the issue was detected.
        metric: The metric that triggered the alert (e.g. ``cpu``, ``disk``, ``service_status``).
        value: The observed value (e.g. ``92%``, ``crashed``).
        severity: Alert severity level.
        context: Description of what was observed.
        recommended_action: What the monitoring agent suggests.
    """

    host: str
    metric: str
    value: str
    severity: Literal["low", "medium", "high", "critical"]
    context: str
    recommended_action: str


class MonitoringResult(BaseModel):
    """Structured output from the monitoring agent.

    Attributes:
        alerts: List of alerts detected during monitoring.
        cluster_healthy: Whether all hosts are healthy.
        dispatched_run_ids: Run IDs of diagnostic agent dispatches triggered by
            the monitoring loop. Only populated by MonitoringLoop background
            dispatch, not by direct /v1/run calls.
    """

    alerts: list[MonitoringAlert] = Field(default_factory=list)
    cluster_healthy: bool
    dispatched_run_ids: list[str] = Field(default_factory=list)


class RunStatus(str, Enum):
    """Status of an async agent run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunResponse(BaseModel):
    """Response body from POST /v1/run on an agent pod.

    Attributes:
        output: The agent's structured result as a dict.
        output_type: Name of the output schema (e.g. ``DiagnosticReport``).
        schema_version: Version of the response envelope format.
        usage: Token usage counts with ``input_tokens`` and ``output_tokens`` keys.
        agent_name: Identifier of the agent that produced this response.
        success: Whether the agent run completed successfully.
        error: Error message if the run failed.
    """

    output: dict[str, Any]
    output_type: str
    schema_version: str = "v1"
    usage: dict[str, Any]
    agent_name: str
    success: bool
    error: Optional[str] = None


class RunState(BaseModel):
    """State of an async agent run stored in the RunStore.

    Attributes:
        run_id: Unique identifier for this run.
        status: Current status of the run.
        result: The agent's response, available when completed or failed.
        created_at: ISO timestamp when the run was submitted.
    """

    run_id: str
    status: RunStatus
    result: Optional[AgentRunResponse] = None
    created_at: str
