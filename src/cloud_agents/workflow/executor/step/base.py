"""Step executor interface for tiered sandbox execution.

Defines the contract between the workflow orchestrator and step
execution backends. Each spawn mode (none, local, ephemeral) has
its own StepExecutor implementation.

No temporalio imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass
class StepInput:
    """Input for a step execution.

    Attributes:
        prompt: Interpolated prompt for the agent.
        provider: LLM provider config (name, model, credentials_secret).
        system_prompt: Optional system/instruction prompt.
        output_schema: Optional JSON Schema for structured output.
        tools: Tool names this step is allowed to use.
        context: Prior step outputs keyed by output_key.
        timeout_seconds: Max execution time.
        sandbox_image: Container image for ephemeral mode.
        skills_image: Optional OCI image for skills.
        skills_paths: Optional paths within skills image.
        mcp_servers: Optional MCP server configurations.
        workflow_id: Workflow execution ID.
        step_name: Step name within the workflow.
        output_key: Key for this step's result in workflow state.
    """

    prompt: str
    provider: dict[str, Any]
    system_prompt: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None
    tools: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 600
    sandbox_image: str = "sandbox:latest"
    skills_image: Optional[str] = None
    skills_paths: Optional[list[str]] = None
    mcp_servers: Optional[list[dict[str, Any]]] = None
    workflow_id: str = ""
    step_name: str = ""
    output_key: str = ""
    raw_step: Optional[dict[str, Any]] = None


@dataclass
class StepResult:
    """Result of a step execution.

    Attributes:
        status: Step outcome (completed, failed).
        output: Structured output from the agent.
        error: Error message if failed.
        transcript: Ordered list of execution events (tool calls, reasoning).
        cost_usd: LLM cost for this step.
        input_tokens: Total input tokens consumed.
        output_tokens: Total output tokens generated.
        duration_ms: Total execution time in milliseconds.
    """

    status: Literal["completed", "failed", "skipped"]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class StepExecutor(ABC):
    """Abstract interface for step execution backends.

    Each spawn mode implements this interface:
    - DirectExecutor (spawn: none) — in-process LLM call or Agent with tools
    - SubprocessExecutor (spawn: local) — LLM call or Agent in subprocess
    - SandboxExecutor (spawn: ephemeral) — OpenShell container
    """

    @abstractmethod
    async def run(self, step_input: StepInput) -> StepResult:
        """Execute a step and return the result with transcript.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with status, output, transcript, and metrics.
        """
