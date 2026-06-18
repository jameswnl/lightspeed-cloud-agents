"""Shared data models for cloud agent HTTP communication.

These models define the contract between the core pod and agent pods.
AgentRunRequest/AgentRunResponse are the HTTP request/response bodies
for the agent runtime's /v1/run endpoint.
"""

from __future__ import annotations

from typing import Any, Optional

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


class DiagnosticReport(BaseModel):
    """Structured output from the diagnostic agent.

    Attributes:
        summary: Brief description of what was found and done.
        issues_found: List of issues discovered during diagnosis.
        actions_taken: Remediation actions attempted.
        remaining_issues: Issues that could not be resolved.
        cluster_healthy: Whether all hosts are healthy after remediation.
    """

    summary: str
    issues_found: list[str]
    actions_taken: list[RemediationAction]
    remaining_issues: list[str] = Field(default_factory=list)
    cluster_healthy: bool


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
