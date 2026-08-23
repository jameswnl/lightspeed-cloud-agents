"""Child process entry point for SubprocessExecutor.

Run via: python -m cloud_agents.workflow.executor.step.subprocess_child

Reads StepInput JSON from stdin, executes LLM call via pydantic-ai
(model_request or Agent with tools), writes StepResult JSON to stdout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest

from cloud_agents.workflow.executor.step.provider import (
    ensure_credentials_env,
    to_model_string,
)
from cloud_agents.workflow.executor.step.tools import get_tools


def main() -> None:
    """Entry point for subprocess child."""
    raw = sys.stdin.read()
    input_data = json.loads(raw)

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
    """Execute the LLM call, with or without tools.

    When tools are present, creates a pydantic-ai Agent with tools from
    the registry. Otherwise uses the simpler model_request path.

    Parameters:
        input_data: Deserialized step input dict from stdin.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    tool_names = input_data.get("tools", [])

    if tool_names:
        return await _run_with_agent(input_data, tool_names)
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


def _parse_content(content: str | None, output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Parse agent/LLM response content into a result dict.

    Parameters:
        content: Raw response string (may be None).
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

    output: dict[str, Any] | None
    try:
        output = json.loads(content)
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


async def _run_with_agent(input_data: dict[str, Any], tool_names: list[str]) -> dict[str, Any]:
    """Execute using pydantic-ai Agent with tools.

    Parameters:
        input_data: Deserialized step input dict.
        tool_names: List of tool names to load from the registry.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    provider = input_data["provider"]
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    system_prompt = input_data.get("system_prompt")
    output_schema = input_data.get("output_schema")
    tools = get_tools(tool_names)

    user_content = _build_user_content(input_data)

    agent = Agent(
        model_string,
        instructions=system_prompt,
        tools=tools,
    )

    result = await agent.run(user_content)

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
