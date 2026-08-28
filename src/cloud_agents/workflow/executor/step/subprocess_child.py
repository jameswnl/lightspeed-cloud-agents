"""Child process entry point for SubprocessExecutor.

Run via: python -m cloud_agents.workflow.executor.step.subprocess_child

Reads StepInput JSON from stdin, executes LLM call via pydantic-ai
(model_request or Agent with tools), writes StepResult JSON to stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport
from pydantic_ai.messages import ModelRequest

from cloud_agents.workflow.executor.step.provider import (
    ensure_credentials_env,
    to_model_string,
)
from cloud_agents.workflow.executor.step.skills import get_skills_capability
from cloud_agents.workflow.executor.step.tools import get_tools, load_tools_module

_TOOLS_MODULE_ENV = "CLOUD_AGENTS_TOOLS_MODULE"


def main() -> None:
    """Entry point for subprocess child."""
    raw = sys.stdin.read()
    input_data = json.loads(raw)

    tools_module = input_data.get("tools_module") or os.environ.get(_TOOLS_MODULE_ENV)
    if tools_module:
        input_data["tools_module"] = tools_module

    start = time.monotonic()
    try:
        result = asyncio.run(_run(input_data))
    except Exception as exc:
        result = {
            "status": "failed",
            "error": str(exc),
            "output": None,
            "transcript": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }

    result["duration_ms"] = int((time.monotonic() - start) * 1000)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


async def _run(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute the LLM call, with or without tools/MCP servers.

    When tools or MCP servers are present, creates a pydantic-ai Agent.
    Otherwise uses the simpler model_request path.

    Parameters:
        input_data: Deserialized step input dict from stdin.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    tool_names = input_data.get("tools", [])
    mcp_servers = input_data.get("mcp_servers") or []
    allowed_skills = input_data.get("allowed_skills")

    skills_cap = get_skills_capability(include=allowed_skills) if allowed_skills is not None else None
    if tool_names or mcp_servers or skills_cap:
        return await _run_with_agent(input_data, tool_names, skills_cap)
    return await _run_model_request(input_data)


def _build_user_content(input_data: dict[str, Any]) -> str:
    """Build user prompt string from input data.

    Parameters:
        input_data: Deserialized step input dict.

    Returns:
        Concatenated user prompt string.
    """
    prompt = input_data["prompt"]
    output_schema = input_data.get("output_schema")
    context = input_data.get("context", {})

    user_content = prompt
    if context:
        context_parts = []
        for key, value in context.items():
            if isinstance(value, dict) and "output" in value:
                context_parts.append(
                    f"Previous step '{key}': {json.dumps(value['output'], indent=2)}"
                )
        if context_parts:
            context_block = "\n\n".join(context_parts)
            user_content = f"{user_content}\n\n--- Prior step outputs ---\n{context_block}"
    if output_schema:
        schema_text = json.dumps(output_schema, indent=2)
        user_content += f"\n\nRespond with valid JSON matching this schema:\n{schema_text}"

    return user_content


_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _strip_markdown_fence(content: str) -> str:
    """Strip a wrapping ```json ... ``` / ``` ... ``` code fence, if present.

    Mirrors direct.py#_strip_markdown_fence -- models asked for JSON via
    prompt text alone (no provider-native JSON mode) commonly wrap it in
    a markdown fence anyway, in this subprocess just as often as in the
    in-process DirectExecutor path.

    Parameters:
        content: Raw LLM response text.

    Returns:
        Fence-stripped content, or the original string if no fence is found.
    """
    match = _MARKDOWN_FENCE_RE.match(content.strip())
    return match.group(1).strip() if match else content


def _parse_content(content: Any, output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Parse agent/LLM response content into a result dict.

    Parameters:
        content: Response from LLM or Agent (str, dict, or None).
        output_schema: Expected JSON Schema, if any.

    Returns:
        Result dict with status, output, transcript, and token fields.
    """
    if content is None:
        if output_schema:
            return {
                "status": "failed",
                "error": "LLM returned null content but output_schema was requested",
                "output": None,
            }
        return {
            "status": "completed",
            "output": {"response": None},
        }

    if isinstance(content, dict):
        return {"status": "completed", "output": content}

    if not isinstance(content, str):
        return {"status": "completed", "output": {"response": str(content)}}

    output: dict[str, Any] | None
    try:
        output = json.loads(_strip_markdown_fence(content))
    except (json.JSONDecodeError, TypeError):
        if output_schema:
            return {
                "status": "failed",
                "error": (
                    f"LLM returned non-JSON response but output_schema was specified: "
                    f"{content[:200]}"
                ),
                "output": None,
            }
        output = {"response": content}

    return {
        "status": "completed",
        "output": output,
    }


async def _run_with_agent(
    input_data: dict[str, Any], tool_names: list[str], skills_cap: Any | None = None
) -> dict[str, Any]:
    """Execute using pydantic-ai Agent with tools and/or MCP servers.

    When MCP servers are configured, creates MCPToolset instances using
    AsyncExitStack for proper lifecycle management.

    Parameters:
        input_data: Deserialized step input dict.
        tool_names: List of tool names to load from the registry.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    tools_module = input_data.get("tools_module")
    if tools_module:
        load_tools_module(tools_module)

    provider = input_data["provider"]
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    system_prompt = input_data.get("system_prompt")
    output_schema = input_data.get("output_schema")
    tools = get_tools(tool_names) if tool_names else []

    # Build MCP toolsets
    mcp_servers = input_data.get("mcp_servers") or []
    mcp_toolsets: list[MCPToolset] = []
    for server in mcp_servers:
        url = server.get("url", "")
        headers = server.get("headers")
        transport = StreamableHttpTransport(url=url, headers=headers)
        mcp_toolsets.append(MCPToolset(transport))

    user_content = _build_user_content(input_data)

    # Use AsyncExitStack for proper MCPToolset lifecycle management
    async with contextlib.AsyncExitStack() as stack:
        active_toolsets = []
        for ts in mcp_toolsets:
            active_ts = await stack.enter_async_context(ts)
            active_toolsets.append(active_ts)

        # Build capabilities (already resolved in _run; passed through to avoid double call)
        capabilities = []
        if skills_cap is None and input_data.get("allowed_skills") is not None:
            # Fallback for direct _run_with_agent calls in tests that bypass _run
            skills_cap = get_skills_capability(include=input_data.get("allowed_skills"))
        if skills_cap:
            capabilities.append(skills_cap)

        agent = Agent(
            model_string,
            instructions=system_prompt,
            tools=tools,
            toolsets=active_toolsets if active_toolsets else None,
            capabilities=capabilities if capabilities else None,
        )

        timeout_seconds = input_data.get("timeout_seconds", 600)
        result = await agent.run(
            user_content,
            model_settings={"timeout": timeout_seconds},
        )

    content = result.output
    usage = result.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

    parsed = _parse_content(content, output_schema)
    parsed["input_tokens"] = input_tokens
    parsed["output_tokens"] = output_tokens
    parsed.setdefault(
        "transcript",
        [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": content or ""},
        ],
    )

    return parsed


async def _run_model_request(input_data: dict[str, Any]) -> dict[str, Any]:
    """Execute using model_request (no tools, single LLM call).

    Parameters:
        input_data: Deserialized step input dict.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    provider = input_data["provider"]
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    system_prompt = input_data.get("system_prompt")
    output_schema = input_data.get("output_schema")
    timeout_seconds = input_data.get("timeout_seconds", 600)

    user_content = _build_user_content(input_data)

    request = ModelRequest.user_text_prompt(user_content, instructions=system_prompt)
    response = await model_request(
        model_string,
        [request],
        model_settings={"timeout": timeout_seconds},
    )

    content = response.text
    usage = response.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0

    parsed = _parse_content(content, output_schema)
    parsed["input_tokens"] = input_tokens
    parsed["output_tokens"] = output_tokens
    parsed.setdefault(
        "transcript",
        [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": content or ""},
        ],
    )

    return parsed


if __name__ == "__main__":
    main()
