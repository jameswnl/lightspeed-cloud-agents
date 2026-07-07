"""Data models for Temporal workflow execution.

Defines the input/output contracts between the FastAPI API layer,
the Temporal workflow class, and the sandbox activities.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from cloud_agents.workflow.authorization import WorkflowAuthzContext


class SecretHeaderRef(BaseModel):
    """Reference to a K8s Secret key for an MCP auth header.

    Attributes:
        secret_name: Name of the K8s Secret containing the header value.
        key: Key within the Secret to use as the header value.
    """

    secret_name: str
    key: str


class MCPServerConfig(BaseModel):
    """MCP server configuration to inject into sandbox pods.

    Attributes:
        name: Unique name identifying this MCP server.
        url: SSE endpoint URL of the MCP server.
        headers: Optional plain-text headers to send with requests.
        secret_headers: Optional Secret-backed headers encoded as file references.
    """

    name: str
    url: str
    headers: Optional[dict[str, str]] = None
    secret_headers: Optional[dict[str, SecretHeaderRef]] = None


class ProviderConfig(BaseModel):
    """LLM provider configuration for sandbox pods.

    Attributes:
        name: Provider identifier (claude, openai, gemini).
        model: Model name or ID.
        credentials_secret: K8s Secret name or Podman env var name.
        model_provider: Optional model provider override for the sandbox pod.
    """

    name: Literal["claude", "openai", "gemini"]
    model: str
    credentials_secret: str
    model_provider: str | None = None


class SkillsConfig(BaseModel):
    """Skills OCI image configuration.

    Attributes:
        image: OCI image reference for skills.
        paths: Subdirectory paths within the skills image to mount.
    """

    image: str
    paths: list[str] = Field(default_factory=list)


class TranscriptEvent(BaseModel):
    """A single event from the agent's execution transcript.

    Attributes:
        ts: ISO timestamp of the event.
        type: Event type discriminator.
        data: Event-specific payload.
    """

    ts: str
    type: Literal["tool_call", "tool_result", "thinking", "result", "error"]
    data: dict[str, Any] = Field(default_factory=dict)


class StepTranscript(BaseModel):
    """Transcript of an agent step's multi-turn execution.

    Captures tool calls, thinking, results, errors, and cost/token
    metrics from the agent's execution loop inside a sandbox.

    Attributes:
        step_name: Name of the workflow step.
        events: Ordered list of transcript events.
        cost_usd: Total LLM cost for this step in USD.
        input_tokens: Total input tokens consumed.
        output_tokens: Total output tokens generated.
        duration_ms: Total step execution time in milliseconds.
    """

    step_name: str
    events: list[TranscriptEvent] = Field(default_factory=list)
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    duration_ms: Optional[int] = None

    def truncate(
        self,
        max_events: int = 20,
        max_payload_bytes: int = 256,
    ) -> StepTranscript:
        """Return a truncated copy suitable for Temporal memo storage.

        Smart truncation strategy: keeps tool names and durations but
        drops large input/output payloads. If the event count exceeds
        max_events, keeps the first half and last half with a marker.

        Parameters:
            max_events: Maximum number of events to keep.
            max_payload_bytes: Maximum bytes for individual payload fields.

        Returns:
            A new StepTranscript with truncated events.
        """
        truncated_events = [
            self._truncate_event(e, max_payload_bytes) for e in self.events
        ]

        if len(truncated_events) <= max_events:
            return StepTranscript(
                step_name=self.step_name,
                events=truncated_events,
                cost_usd=self.cost_usd,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                duration_ms=self.duration_ms,
            )

        half = max_events // 2
        first = truncated_events[:half]
        last = truncated_events[-half:] if half > 0 else []
        omitted = len(truncated_events) - (len(first) + len(last))
        marker = TranscriptEvent(
            ts="",
            type="result",
            data={"_truncated": True, "omitted_events": omitted},
        )
        return StepTranscript(
            step_name=self.step_name,
            events=first + [marker] + last,
            cost_usd=self.cost_usd,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            duration_ms=self.duration_ms,
        )

    @staticmethod
    def _truncate_event(
        event: TranscriptEvent,
        max_payload_bytes: int,
    ) -> TranscriptEvent:
        """Truncate large payload fields in an event.

        Keeps name and duration_ms intact; truncates input/output
        strings that exceed max_payload_bytes.

        Parameters:
            event: The transcript event to truncate.
            max_payload_bytes: Max bytes for individual payload fields.

        Returns:
            A new TranscriptEvent with truncated data.
        """
        data = dict(event.data)
        for key in ("input", "output", "text"):
            if key in data:
                serialized = json.dumps(data[key]) if not isinstance(data[key], str) else data[key]
                if len(serialized) > max_payload_bytes:
                    data[key] = serialized[:max_payload_bytes] + "...(truncated)"
        return TranscriptEvent(ts=event.ts, type=event.type, data=data)


class StepResult(BaseModel):
    """Result of a single workflow step.

    Attributes:
        status: Step outcome.
        output: Structured output from the agent.
        error: Error message on failure.
    """

    status: Literal["completed", "failed", "skipped", "escalated", "denied"] = "pending"
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class WorkflowInput(BaseModel):
    """Input to the generic AgentWorkflow.

    Attributes:
        definition: Parsed workflow YAML as dict.
        input_prompt: User-provided prompt for the workflow.
        workflow_id: Unique run identifier.
        provider: LLM provider configuration.
        sandbox_image: Container image for sandbox pods.
        skills_image: Optional OCI image for skills.
        skills_paths: Optional subdirectory paths in skills image.
        mcp_servers: Optional MCP servers to inject into sandbox pods.
        authz_context: Authorization context captured at trigger time.
    """

    definition: dict[str, Any]
    input_prompt: Optional[str] = None
    workflow_id: str
    provider: ProviderConfig
    sandbox_image: str = "lightspeed-agentic-sandbox:latest"
    skills_image: Optional[str] = None
    skills_paths: Optional[list[str]] = None
    approval_policy: Optional[dict[str, Any]] = None
    advisory: bool = False
    notifier_config: Optional[dict[str, Any]] = None
    escalation_config: Optional[dict[str, Any]] = None
    mcp_servers: Optional[list[MCPServerConfig]] = None
    authz_context: Optional[WorkflowAuthzContext] = None


class WorkflowOutput(BaseModel):
    """Output from a completed workflow.

    Attributes:
        steps: Step results keyed by output_key.
    """

    steps: dict[str, StepResult] = Field(default_factory=dict)


class WorkflowEvent(BaseModel):
    """Event emitted during workflow execution.

    Attributes:
        type: Event type identifier.
        step: Step name that triggered the event.
        timestamp: ISO timestamp of the event.
    """

    type: str
    step: str
    timestamp: str


class WorkflowStatus(BaseModel):
    """Queryable workflow status.

    Attributes:
        steps: Current step results.
        events: Event history.
    """

    steps: dict[str, StepResult] = Field(default_factory=dict)
    events: list[WorkflowEvent] = Field(default_factory=list)


class SandboxStepInput(BaseModel):
    """Input to the run_sandbox_step activity.

    Attributes:
        step: Step specification from the workflow definition.
        workflow_id: Workflow run identifier.
        provider: LLM provider config.
        sandbox_image: Container image for the sandbox pod.
        skills_image: Optional skills OCI image.
        skills_paths: Optional skills subdirectory paths.
        context: Accumulated step results from prior steps.
    """

    step: dict[str, Any]
    workflow_id: str
    provider: ProviderConfig
    sandbox_image: str
    skills_image: Optional[str] = None
    skills_paths: Optional[list[str]] = None
    context: dict[str, Any] = Field(default_factory=dict)
