"""DirectExecutor -- spawn: none step executor.

Executes a single LLM call (no tools) or a pydantic-ai Agent loop
(with tools/MCP servers) depending on the step's tools list and
mcp_servers configuration.

No temporalio imports.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport
from pydantic_ai.messages import ModelRequest

from cloud_agents.workflow.executor.step.base import (
    StepExecutor,
    StepInput,
    StepResult,
    StreamEvent,
)
from cloud_agents.workflow.executor.step.provider import ensure_credentials_env, to_model_string
from cloud_agents.workflow.executor.step.skills import get_skills_capability
from cloud_agents.workflow.executor.step.tools import get_tools

logger = logging.getLogger(__name__)


def _build_messages(step_input: StepInput) -> list[dict[str, str]]:
    """Build chat messages from step input.

    Parameters:
        step_input: Step execution input.

    Returns:
        List of message dicts for the chat completions API.
    """
    messages: list[dict[str, str]] = []

    if step_input.system_prompt:
        messages.append({"role": "system", "content": step_input.system_prompt})

    user_content = step_input.prompt

    if step_input.context:
        context_parts = []
        for key, value in step_input.context.items():
            if isinstance(value, dict) and "output" in value:
                context_parts.append(
                    f"Previous step '{key}': {json.dumps(value['output'], indent=2)}"
                )
        if context_parts:
            context_block = "\n\n".join(context_parts)
            user_content = f"{user_content}\n\n--- Prior step outputs ---\n{context_block}"

    if step_input.output_schema:
        schema_str = json.dumps(step_input.output_schema, indent=2)
        user_content = f"{user_content}\n\nRespond with JSON matching this schema:\n{schema_str}"

    messages.append({"role": "user", "content": user_content})
    return messages


def _build_user_prompt(step_input: StepInput) -> str:
    """Build a single user prompt string from step input.

    Used by the Agent path where instructions (system prompt) are passed
    separately to the Agent constructor.

    Parameters:
        step_input: Step execution input.

    Returns:
        Concatenated user prompt string.
    """
    user_content = step_input.prompt

    if step_input.context:
        context_parts = []
        for key, value in step_input.context.items():
            if isinstance(value, dict) and "output" in value:
                context_parts.append(
                    f"Previous step '{key}': {json.dumps(value['output'], indent=2)}"
                )
        if context_parts:
            context_block = "\n\n".join(context_parts)
            user_content = f"{user_content}\n\n--- Prior step outputs ---\n{context_block}"

    if step_input.output_schema:
        schema_str = json.dumps(step_input.output_schema, indent=2)
        user_content = f"{user_content}\n\nRespond with JSON matching this schema:\n{schema_str}"

    return user_content


async def _call_llm(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Call LLM via pydantic-ai model_request.

    Parameters:
        provider: Provider config (name, model, credentials_secret).
        messages: Chat messages list.
        timeout_seconds: Request timeout.

    Returns:
        Dict with content, input_tokens, output_tokens.

    Raises:
        ValueError: If provider name is unknown.
    """
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_parts = [m["content"] for m in messages if m["role"] == "user"]

    instructions = system_parts[0] if system_parts else None
    user_prompt = "\n\n".join(user_parts)

    request = ModelRequest.user_text_prompt(user_prompt, instructions=instructions)

    response = await model_request(
        model_string,
        [request],
        model_settings={"timeout": timeout_seconds},
    )

    usage = response.usage
    return {
        "content": response.text,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


class DirectExecutor(StepExecutor):
    """Execute steps in-process via pydantic-ai.

    This is the spawn: none implementation. It sends a single chat
    completion request and returns the result. Supports structured
    output via output_schema (JSON mode).
    """

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute a step as a single LLM call or pydantic-ai Agent loop.

        When the step has tools or MCP servers, creates a pydantic-ai Agent
        with tools from the registry and/or MCPToolsets and runs the agent
        loop. Otherwise falls back to the simpler model_request path.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with LLM response, token counts, and transcript.
        """
        start_ms = time.monotonic_ns() // 1_000_000

        try:
            skills_cap = get_skills_capability()
            if step_input.tools or step_input.mcp_servers or skills_cap:
                return await self._run_with_agent(step_input, start_ms)
            return await self._run_model_request(step_input, start_ms)

        except ValueError as exc:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            logger.error(
                "DirectExecutor failed for step '%s': %s",
                step_input.step_name,
                exc,
            )
            return StepResult(
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            logger.error("DirectExecutor failed for step '%s': %s", step_input.step_name, exc)
            return StepResult(
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )

    async def run_stream(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Stream step execution events with real token deltas.

        When the step has tools or MCP servers, uses Agent.run_stream()
        for true token-by-token streaming. Otherwise falls back to the
        default implementation which calls run() and yields a single
        complete event.

        Parameters:
            step_input: Step execution input.

        Yields:
            StreamEvent instances (token deltas followed by complete/error).
        """
        if not (step_input.tools or step_input.mcp_servers):
            async for event in super().run_stream(step_input):
                yield event
            return

        start_ms = time.monotonic_ns() // 1_000_000

        try:
            if step_input.tools_module:
                from cloud_agents.workflow.executor.step.tools import load_tools_module

                load_tools_module(step_input.tools_module)

            ensure_credentials_env(step_input.provider)
            model_string = to_model_string(step_input.provider)
            tools = get_tools(step_input.tools) if step_input.tools else []

            # Build MCP toolsets
            mcp_toolsets: list[MCPToolset] = []
            for server in step_input.mcp_servers or []:
                url = server.get("url", "")
                headers = server.get("headers")
                transport = StreamableHttpTransport(url=url, headers=headers)
                mcp_toolsets.append(MCPToolset(transport))

            user_prompt = _build_user_prompt(step_input)

            async with contextlib.AsyncExitStack() as stack:
                active_toolsets = []
                for ts in mcp_toolsets:
                    active_ts = await stack.enter_async_context(ts)
                    active_toolsets.append(active_ts)

                agent = Agent(
                    model_string,
                    instructions=step_input.system_prompt,
                    tools=tools,
                    toolsets=active_toolsets if active_toolsets else None,
                )

                async with agent.run_stream(
                    user_prompt,
                    model_settings={"timeout": step_input.timeout_seconds},
                ) as streamed:
                    async for delta in streamed.stream_text(delta=True):
                        yield StreamEvent(type="token", data={"delta": delta})

                    output_text = await streamed.get_output()
                    usage = streamed.usage

            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            output = _parse_output(output_text, step_input.output_schema)
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

            transcript = [
                {
                    "type": "agent.stream",
                    "model": step_input.provider.get("model", "unknown"),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "step_name": step_input.step_name,
                    "tools": step_input.tools,
                    "mcp_servers": [
                        s.get("name", "") for s in (step_input.mcp_servers or [])
                    ],
                },
            ]

            step_result = StepResult(
                status="completed",
                output=output,
                transcript=transcript,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
            )
            yield StreamEvent(type="complete", result=step_result)

        except Exception as exc:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            logger.error(
                "DirectExecutor streaming failed for step '%s': %s",
                step_input.step_name,
                exc,
            )
            yield StreamEvent(
                type="error",
                data={"error": str(exc)},
                result=StepResult(status="failed", error=str(exc), duration_ms=duration_ms),
            )

    async def _run_with_agent(self, step_input: StepInput, start_ms: int) -> StepResult:
        """Execute using pydantic-ai Agent with tools and/or MCP servers.

        When MCP servers are configured, creates MCPToolset instances using
        AsyncExitStack for proper lifecycle management. Each server gets its
        own StreamableHttpTransport with optional auth headers.

        Parameters:
            step_input: Step execution input.
            start_ms: Start time in milliseconds for duration tracking.

        Returns:
            StepResult with agent output, token counts, and transcript.
        """
        if step_input.tools_module:
            from cloud_agents.workflow.executor.step.tools import load_tools_module

            load_tools_module(step_input.tools_module)

        ensure_credentials_env(step_input.provider)
        model_string = to_model_string(step_input.provider)
        tools = get_tools(step_input.tools) if step_input.tools else []

        # Build MCP toolsets
        mcp_toolsets: list[MCPToolset] = []
        for server in step_input.mcp_servers or []:
            url = server.get("url", "")
            headers = server.get("headers")
            transport = StreamableHttpTransport(url=url, headers=headers)
            mcp_toolsets.append(MCPToolset(transport))

        user_prompt = _build_user_prompt(step_input)

        # Use AsyncExitStack for proper MCPToolset lifecycle management
        async with contextlib.AsyncExitStack() as stack:
            active_toolsets = []
            for ts in mcp_toolsets:
                active_ts = await stack.enter_async_context(ts)
                active_toolsets.append(active_ts)

            # Build capabilities
            capabilities = []
            skills_cap = get_skills_capability()
            if skills_cap:
                capabilities.append(skills_cap)

            agent = Agent(
                model_string,
                instructions=step_input.system_prompt,
                tools=tools,
                toolsets=active_toolsets if active_toolsets else None,
                capabilities=capabilities if capabilities else None,
            )

            result = await agent.run(
                user_prompt,
                model_settings={"timeout": step_input.timeout_seconds},
            )

        content = result.output
        usage = result.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0

        output = _parse_output(content, step_input.output_schema)

        duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

        mcp_server_names = [s.get("name", "") for s in (step_input.mcp_servers or [])]

        transcript = [
            {
                "type": "agent.run",
                "model": step_input.provider.get("model", "unknown"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "step_name": step_input.step_name,
                "tools": step_input.tools,
                "mcp_servers": mcp_server_names,
            },
        ]

        logger.info(
            "DirectExecutor (agent) completed step '%s' "
            "(%d input, %d output tokens, %dms, tools=%s, mcp_servers=%s)",
            step_input.step_name,
            input_tokens,
            output_tokens,
            duration_ms,
            step_input.tools,
            mcp_server_names,
        )

        return StepResult(
            status="completed",
            output=output,
            transcript=transcript,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )

    async def _run_model_request(self, step_input: StepInput, start_ms: int) -> StepResult:
        """Execute using model_request (no tools, single LLM call).

        Parameters:
            step_input: Step execution input.
            start_ms: Start time in milliseconds for duration tracking.

        Returns:
            StepResult with LLM response, token counts, and transcript.
        """
        messages = _build_messages(step_input)

        llm_result = await _call_llm(
            provider=step_input.provider,
            messages=messages,
            timeout_seconds=step_input.timeout_seconds,
        )

        content = llm_result["content"]
        input_tokens = llm_result["input_tokens"]
        output_tokens = llm_result["output_tokens"]

        output = _parse_output(content, step_input.output_schema)

        duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms

        transcript = [
            {
                "type": "llm.call",
                "model": step_input.provider.get("model", "unknown"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "step_name": step_input.step_name,
            },
        ]

        logger.info(
            "DirectExecutor completed step '%s' (%d input, %d output tokens, %dms)",
            step_input.step_name,
            input_tokens,
            output_tokens,
            duration_ms,
        )

        return StepResult(
            status="completed",
            output=output,
            transcript=transcript,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )


def _parse_output(content: Any, output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Parse LLM response content into structured output.

    Parameters:
        content: LLM response (usually str, may be None or dict).
        output_schema: Expected JSON Schema, if any.

    Returns:
        Parsed dict output.

    Raises:
        ValueError: If output_schema is set but response is not valid JSON.
    """
    if content is None:
        if output_schema:
            raise ValueError("LLM returned null content but output_schema was requested")
        return {"response": None}

    if isinstance(content, dict):
        return content

    if not isinstance(content, str):
        return {"response": str(content)}

    if output_schema:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(
                f"LLM returned non-JSON response but output_schema was requested: {content[:200]}"
            )

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"response": content}
