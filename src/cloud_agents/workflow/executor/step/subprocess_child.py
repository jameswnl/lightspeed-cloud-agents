"""Child process entry point for SubprocessExecutor.

Run via: python -m cloud_agents.workflow.executor.step.subprocess_child

Reads StepInput JSON from stdin, executes LLM call via pydantic-ai,
writes StepResult JSON to stdout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest

from cloud_agents.workflow.executor.step.provider import (
    ensure_credentials_env,
    to_model_string,
)


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
    """Execute the LLM call.

    Parameters:
        input_data: Deserialized step input dict from stdin.

    Returns:
        Result dict with status, output, transcript, and token counts.
    """
    provider = input_data["provider"]
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    prompt = input_data["prompt"]
    system_prompt = input_data.get("system_prompt")
    output_schema = input_data.get("output_schema")
    context = input_data.get("context", {})

    # Build user message with context and schema instructions
    user_content = prompt
    if context:
        context_text = json.dumps(context, indent=2)
        user_content = f"Prior step results:\n{context_text}\n\n{prompt}"
    if output_schema:
        schema_text = json.dumps(output_schema, indent=2)
        user_content += f"\n\nRespond with valid JSON matching this schema:\n{schema_text}"

    request = ModelRequest.user_text_prompt(user_content, instructions=system_prompt)
    response = await model_request(model_string, [request])

    content = response.text or ""
    usage = response.usage

    # Parse output
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
                "transcript": [{"role": "assistant", "content": content}],
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
        output = {"response": content}

    return {
        "status": "completed",
        "output": output,
        "transcript": [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": content},
        ],
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


if __name__ == "__main__":
    main()
