"""WorkflowDefinition model — Pydantic schema for workflow.yaml.

Defines the YAML contract for multi-step agent workflows.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from agents.spawner.base import SpawnConfig
from agents.workflow.permissions import PermissionScope


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
    spawn: Literal["pre-deployed", "on-demand", "ephemeral"] = "ephemeral"
    risk_level: Optional[Literal["low", "medium", "high", "critical"]] = None
    permissions: Optional[PermissionScope] = None
    parallel_group: Optional[str] = None
    spawn_config: Optional[SpawnConfig] = None
    runtime: Literal["sandbox", "generic"] = "sandbox"
    role: Optional[Literal["analysis", "execution", "verification"]] = None
    instructions: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None
    service_account: Optional[str] = None
    target_namespaces: Optional[list[str]] = None


class WorkflowSpec(BaseModel):
    """Full workflow specification.

    Attributes:
        input_prompt: Optional initial prompt passed to the first step.
        steps: Ordered list of workflow steps.
    """

    input_prompt: Optional[str] = None
    steps: list[WorkflowStepSpec] = Field(..., min_length=1)


class ProviderSpec(BaseModel):
    """Provider configuration at the workflow level.

    Attributes:
        name: Provider name (openai, claude, gemini).
        model: Model identifier.
        credentials_secret: K8s secret name or env var prefix for credentials.
    """

    name: str
    model: str
    credentials_secret: str


class SkillsSpec(BaseModel):
    """Skills configuration for sandbox agents.

    Attributes:
        image: OCI image containing skills.
        paths: Paths within the image to mount.
    """

    image: str
    paths: Optional[list[str]] = None


class WorkflowDefinition(BaseModel):
    """Top-level workflow definition from workflow.yaml.

    Attributes:
        apiVersion: API version string.
        kind: Must be AgentWorkflow.
        metadata: Workflow metadata including name.
        spec: Full workflow specification.
        provider: Default provider for all steps.
        skills: Skills OCI image configuration.
    """

    apiVersion: str
    kind: Literal["AgentWorkflow"]
    metadata: dict[str, Any]
    spec: WorkflowSpec
    provider: Optional[ProviderSpec] = None
    skills: Optional[SkillsSpec] = None
    advisory: bool = False
