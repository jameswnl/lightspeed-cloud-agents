"""Data models for Temporal workflow execution.

Defines the input/output contracts between the FastAPI API layer,
the Temporal workflow class, and the sandbox activities.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


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
    """

    name: Literal["claude", "openai", "gemini"]
    model: str
    credentials_secret: str


class SkillsConfig(BaseModel):
    """Skills OCI image configuration.

    Attributes:
        image: OCI image reference for skills.
        paths: Subdirectory paths within the skills image to mount.
    """

    image: str
    paths: list[str] = Field(default_factory=list)


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
