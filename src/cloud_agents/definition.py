"""AgentDefinition model — Pydantic schema for agent.yaml.

Defines the YAML contract for configuring agents in the generic runtime.
Each agent pod reads an agent.yaml at startup and constructs the
Pydantic AI Agent from this definition.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ToolsSpec(BaseModel):
    """Specification for agent tool loading.

    Attributes:
        module: Python module name importable from /app/tools/.
        functions: Function names to register as agent.tool_plain().
        read_only: Functions that are safe in advisory mode (no side effects).
    """

    module: str = Field(..., min_length=1)
    functions: list[str] = Field(..., min_length=1)
    read_only: list[str] = Field(default_factory=list)

    @field_validator("read_only")
    @classmethod
    def validate_read_only_subset(cls, v: list[str], info: Any) -> list[str]:
        """Validate that read_only tools are a subset of functions."""
        functions = info.data.get("functions", [])
        invalid = set(v) - set(functions)
        if invalid:
            raise ValueError(
                f"read_only tools not in functions list: {sorted(invalid)}"
            )
        return v


class OutputValidatorSpec(BaseModel):
    """Specification for an output validator function.

    Attributes:
        module: Python module containing the validator.
        function: Function name with signature (RunContext, T) -> T.
    """

    module: str
    function: str


class OnDispatchSuccessSpec(BaseModel):
    """Specification for a post-dispatch callback.

    Attributes:
        module: Python module containing the callback.
        function: Function name with signature (alerts) -> None.
    """

    module: str
    function: str


class LifecycleSpec(BaseModel):
    """Lifecycle configuration for the agent.

    Attributes:
        type: Agent lifecycle type.
        interval_seconds: Polling interval for periodic-loop agents.
        dispatch_to: Agent name to dispatch to (resolved via AgentRegistry).
        on_dispatch_success: Optional post-dispatch callback.
    """

    type: Literal["request-response", "periodic-loop"]
    interval_seconds: int = 300
    dispatch_to: Optional[str] = None
    on_dispatch_success: Optional[OnDispatchSuccessSpec] = None


class SkillsSpec(BaseModel):
    """Specification for agent skills.

    Attributes:
        directories: Paths to scan for SKILL.md files.
    """

    directories: list[str] = Field(default_factory=list)


class MCPAuthSpec(BaseModel):
    """Authentication specification for MCP servers.

    Attributes:
        type: Auth type — env_var (production) or header_value (dev/test only).
        env_var: Environment variable name containing the auth token.
        header_value: Inline header value (dev/test only, logged with warning).
        header_name: HTTP header name for the token.
        header_prefix: Prefix before the token value (e.g. "Bearer ").
    """

    type: Literal["env_var", "header_value"]
    env_var: Optional[str] = None
    header_value: Optional[str] = None
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "


class MCPServerSpec(BaseModel):
    """Specification for an MCP server connection.

    Attributes:
        name: Human-readable name for the MCP server.
        url: HTTP endpoint URL of the MCP server.
        auth: Optional authentication configuration.
    """

    name: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    auth: Optional[MCPAuthSpec] = None


class AgentResourceSpec(BaseModel):
    """Resource limits for an agent.

    Attributes:
        max_tokens_per_run: Maximum token budget per run.
        timeout_seconds: Maximum run duration in seconds.
    """

    max_tokens_per_run: int = 50000
    timeout_seconds: int = 600


class AgentSpec(BaseModel):
    """Full agent specification from agent.yaml.

    Attributes:
        instructions: System prompt / instructions for the agent.
        output_type: Output type class name (built-in or from output_type_module).
        output_type_module: Optional Python module for custom output types.
        retries: Number of retries on output validation failure.
        defer_model_check: Whether to skip model name validation.
        tools: Tool loading specification.
        skills: Optional skills configuration.
        lifecycle: Agent lifecycle configuration.
        output_validator: Optional output validator specification.
        model: Optional model override.
        resources: Optional resource limits.
    """

    instructions: str
    output_type: str
    output_type_module: Optional[str] = None
    retries: int = 1
    defer_model_check: bool = True
    tools: ToolsSpec
    mcp_servers: Optional[list[MCPServerSpec]] = None
    skills: Optional[SkillsSpec] = None
    lifecycle: LifecycleSpec
    output_validator: Optional[OutputValidatorSpec] = None
    model: Optional[dict[str, str]] = None
    resources: Optional[AgentResourceSpec] = None


class AgentDefinition(BaseModel):
    """Top-level AgentDefinition from agent.yaml.

    Attributes:
        apiVersion: API version string.
        kind: Must be ``AgentDefinition``.
        metadata: Agent metadata including name.
        spec: Full agent specification.
    """

    apiVersion: str
    kind: Literal["AgentDefinition"]
    metadata: dict[str, Any]
    spec: AgentSpec
