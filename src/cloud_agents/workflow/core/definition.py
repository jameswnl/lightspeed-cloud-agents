"""WorkflowDefinition model — Pydantic schema for workflow.yaml.

Defines the YAML contract for multi-step agent workflows.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, field_validator

from cloud_agents.spawner.base import SpawnConfig
from cloud_agents.workflow.core.execution import InferenceProviderSpec
from cloud_agents.workflow.core.models import MCPServerConfig
from cloud_agents.workflow.core.permissions import PermissionScope


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
        allowed_skills: Optional list of skill names (subdirectories baked
            into the sandbox image's /skills or local CLOUD_AGENTS_SKILLS_PATHS).
            For spawn: none/local, enforced via SkillsCapability(include=[...]);
            for spawn: ephemeral, via a per-spawn Landlock read-only grant on
            /skills/<name> (see OpenShellSpawner). Other spawners
            (Kubernetes/Podman) accept but do not enforce this field.
            ChatWorkflowConfig.allowed_skills threads the same allowlist for
            chat turns. None or omitted (and []) means no skills are visible --
            least-privilege default, not "all" -- except in OpenShellSpawner's
            advisory (read_only) mode, which grants blanket filesystem read for
            investigation purposes regardless of this field. Unlike
            mcp_servers, there's no workflow-level catalog to select from --
            the catalog is whatever the deployed sandbox image or local
            CLOUD_AGENTS_SKILLS_PATHS happens to provide.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    type: Literal["agent", "human-approval"] = "agent"
    agent: Optional[str] = None
    prompt: Optional[str] = None
    output_key: Optional[str] = None
    condition: Optional[str] = None
    message: Optional[str] = None
    timeout_seconds: Optional[int] = None
    max_retries: NonNegativeInt = 0
    spawn: Optional[Literal["none", "local", "ephemeral"]] = None
    risk_level: Optional[Literal["low", "medium", "high", "critical"]] = None
    permissions: Optional[PermissionScope] = None
    parallel_group: Optional[str] = None
    mcp_servers: Optional[list[str | MCPServerConfig]] = None
    spawn_config: Optional[SpawnConfig] = None
    runtime: Literal["sandbox", "generic"] = "sandbox"
    role: Optional[Literal["analysis", "execution", "verification"]] = None
    instructions: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None
    tools: list[str] = Field(default_factory=list)
    context: Optional[dict[str, Any]] = None
    service_account: Optional[str] = None
    target_namespaces: Optional[list[str]] = None
    allowed_skills: Optional[list[str]] = None
    inference_provider: Optional[InferenceProviderSpec] = None


class WorkflowSpec(BaseModel):
    """Full workflow specification.

    Attributes:
        input_prompt: Optional initial prompt passed to the first step.
        steps: Ordered list of workflow steps.
        context: Optional workflow-level execution context merged with
            per-step ``context`` (step values win per key).
    """

    model_config = ConfigDict(extra="forbid")

    input_prompt: Optional[str] = None
    steps: list[WorkflowStepSpec] = Field(..., min_length=1)
    timeout_seconds: Optional[int] = None
    spawn: Optional[Literal["none", "local", "ephemeral"]] = None
    spawn_config: Optional[SpawnConfig] = None
    mcp_servers: Optional[list[str | MCPServerConfig]] = None
    allowed_skills: Optional[list[str]] = None
    permissions: Optional[PermissionScope] = None
    service_account: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    escalation: Optional[dict[str, Any]] = None


class ProviderSpec(BaseModel):
    """Provider configuration at the workflow level.

    Attributes:
        name: Provider name (openai, claude, gemini).
        model: Model identifier.
        credentials_secret: Optional K8s secret name or env var prefix
            for credentials. Omit it for the issue-#268 contract;
            runtime credentials resolve separately (see ProviderConfig).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    credentials_secret: Optional[str] = None

    @field_validator("credentials_secret")
    @classmethod
    def _validate_secret_reference(cls, value: Optional[str]) -> Optional[str]:
        """Reject secret values in the definition provider reference."""
        if value is None:
            return None
        from cloud_agents.workflow.core.execution import validate_credential_reference

        return validate_credential_reference(value)


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

    model_config = ConfigDict(extra="forbid")

    apiVersion: str
    kind: Literal["AgentWorkflow"]
    metadata: dict[str, Any]
    spec: WorkflowSpec
    provider: Optional[ProviderSpec] = None
    skills: Optional[SkillsSpec] = None
    advisory: bool = False
