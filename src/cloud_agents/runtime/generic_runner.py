"""Generic agent runner — builds and runs a Pydantic AI Agent from an AgentSpec.

Replaces the per-agent run_diagnostic() and run_monitoring() functions
with a single generic runner that works for any agent definition.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import Agent

from pathlib import Path

from agents.definition import AgentSpec
from agents.models import AgentRunRequest, AgentRunResponse
from agents.runtime.mcp_loader import load_mcp_servers
from agents.runtime.output_types import resolve_output_type
from agents.runtime.tool_instrumentation import instrument_tool
from agents.runtime.tool_loader import load_tools


def _load_skills(spec: AgentSpec) -> list:
    """Load skills from the agent spec. Strict — fails if configured but unavailable."""
    if not spec.skills or not spec.skills.directories:
        return []
    try:
        from pydantic_ai_skills import SkillsCapability
    except ImportError as exc:
        raise RuntimeError(
            "Skills are configured in agent.yaml but pydantic-ai-skills "
            "is not installed. Install it or remove the skills section."
        ) from exc
    for d in spec.skills.directories:
        if not Path(d).is_dir():
            logger.warning("Skills directory not found: %s (skipping)", d)
    valid_dirs = [d for d in spec.skills.directories if Path(d).is_dir()]
    if not valid_dirs:
        raise RuntimeError(
            f"Skills are configured but none of the directories exist: "
            f"{spec.skills.directories}. Fix the paths or remove the skills section."
        )
    return [SkillsCapability(directories=valid_dirs)]

logger = logging.getLogger(__name__)


def create_generic_runner(
    spec: AgentSpec,
    model: Any,
    agent_name: str,
) -> Callable[[AgentRunRequest], Awaitable[AgentRunResponse]]:
    """Create an agent runner function from an AgentSpec.

    Builds a Pydantic AI Agent with the specified tools, output type,
    instructions, and retries. Returns an async callable suitable for
    passing to create_app(agent_runner=...).

    When the request context contains advisory_mode=true and the agent
    has read_only tools classified, only read-only tools are registered.

    Args:
        spec: The agent specification from agent.yaml.
        model: The Pydantic AI model (from model_factory or FunctionModel).
        agent_name: Agent identifier for logging and response envelope.

    Returns:
        Async callable that processes AgentRunRequest → AgentRunResponse.
    """
    output_type = resolve_output_type(spec.output_type, spec.output_type_module)
    all_tools = load_tools(spec.tools)
    read_only_tools = set(spec.tools.read_only) if spec.tools.read_only else set()
    mcp_servers = load_mcp_servers(spec.mcp_servers) if spec.mcp_servers else []
    capabilities = _load_skills(spec)

    def _build_agent(advisory_mode: bool = False) -> Agent[None, Any]:
        """Build the agent, optionally filtering tools for advisory mode."""
        mcp_tools = mcp_servers if mcp_servers else None
        agent: Agent[None, Any] = Agent(
            model,
            output_type=output_type,
            retries=spec.retries,
            defer_model_check=spec.defer_model_check,
            instructions=spec.instructions,
            capabilities=capabilities or None,
            mcp_servers=mcp_tools,
        )

        tools_to_register = all_tools
        if advisory_mode and read_only_tools:
            tools_to_register = [(n, f) for n, f in all_tools if n in read_only_tools]
            removed = [n for n, _ in all_tools if n not in read_only_tools]
            if removed:
                logger.info("Advisory mode: filtered out tools: %s", removed)
        elif advisory_mode and not read_only_tools:
            logger.warning(
                "Advisory mode requested but no read_only tools classified for %s. "
                "All tools remain available.", agent_name,
            )

        for fn_name, fn in tools_to_register:
            instrumented = instrument_tool(fn, agent_name, fn_name)
            agent.tool_plain(instrumented, docstring_format="google")

        if spec.output_validator:
            import importlib
            val_mod = importlib.import_module(spec.output_validator.module)
            val_fn = getattr(val_mod, spec.output_validator.function)
            agent.output_validator(val_fn)

        return agent

    default_agent = _build_agent(advisory_mode=False)
    advisory_agent: Agent[None, Any] | None = None
    if read_only_tools:
        advisory_agent = _build_agent(advisory_mode=True)

    async def run_agent(request: AgentRunRequest) -> AgentRunResponse:
        """Run the agent and return a structured response."""
        correlation_id = (request.context or {}).get("correlation_id", "none")
        is_advisory = (request.context or {}).get("advisory_mode", False)
        logger.info(
            "Starting generic run",
            extra={"agent_name": agent_name, "correlation_id": correlation_id},
        )

        active_agent = (advisory_agent if is_advisory and advisory_agent else default_agent)

        try:
            result = await active_agent.run(request.prompt)
        except Exception as exc:
            logger.error(
                "Run failed: %s",
                exc,
                extra={"agent_name": agent_name, "correlation_id": correlation_id},
            )
            return AgentRunResponse(
                output={},
                output_type="error",
                usage={"input_tokens": 0, "output_tokens": 0},
                agent_name=agent_name,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        output_data: dict[str, Any]
        if hasattr(result.output, "model_dump"):
            output_data = result.output.model_dump()
        elif isinstance(result.output, str):
            output_data = {"text": result.output}
        else:
            output_data = {"value": result.output}

        return AgentRunResponse(
            output=output_data,
            output_type=output_type.__name__,
            usage={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
            agent_name=agent_name,
            success=True,
        )

    return run_agent
