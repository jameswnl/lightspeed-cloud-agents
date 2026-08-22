"""DirectExecutor — spawn: none LLM-only step executor.

Executes a single LLM call with no tools or agent loop.
Uses pydantic-ai model_request for provider-agnostic LLM access.

No temporalio imports.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest

from cloud_agents.workflow.executor.step.base import StepExecutor, StepInput, StepResult
from cloud_agents.workflow.executor.step.provider import ensure_credentials_env, to_model_string

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


async def _call_llm(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    output_schema: dict[str, Any] | None = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Call LLM via pydantic-ai model_request.

    Parameters:
        provider: Provider config (name, model, credentials_secret).
        messages: Chat messages list.
        output_schema: Optional JSON Schema for structured output.
        timeout_seconds: Request timeout.

    Returns:
        Dict with content, input_tokens, output_tokens.

    Raises:
        ValueError: If provider name is unknown.
    """
    ensure_credentials_env(provider)
    model_string = to_model_string(provider)

    # Extract system and user parts from messages
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_parts = [m["content"] for m in messages if m["role"] == "user"]

    instructions = system_parts[0] if system_parts else None
    user_prompt = "\n\n".join(user_parts)

    request = ModelRequest.user_text_prompt(user_prompt, instructions=instructions)

    response = await model_request(model_string, [request])

    usage = response.usage
    return {
        "content": response.text,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


class DirectExecutor(StepExecutor):
    """Execute steps as direct LLM calls with no tools or agent loop.

    This is the spawn: none implementation. It sends a single chat
    completion request and returns the result. Supports structured
    output via output_schema (JSON mode).
    """

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute a step as a single LLM call.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with LLM response, token counts, and transcript.
        """
        start_ms = time.monotonic_ns() // 1_000_000

        if step_input.tools:
            logger.warning(
                "DirectExecutor (spawn: none) ignores tools %s for step '%s'. "
                "Use spawn: local or spawn: ephemeral for tool support.",
                step_input.tools,
                step_input.step_name,
            )

        try:
            messages = _build_messages(step_input)

            llm_result = await _call_llm(
                provider=step_input.provider,
                messages=messages,
                output_schema=step_input.output_schema,
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

        except ValueError as exc:
            duration_ms = (time.monotonic_ns() // 1_000_000) - start_ms
            logger.error(
                "DirectExecutor credential error for step '%s': %s",
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
            logger.error(
                "DirectExecutor failed for step '%s': %s", step_input.step_name, exc
            )
            return StepResult(
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
            )


def _parse_output(content: str, output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Parse LLM response content into structured output.

    Parameters:
        content: Raw LLM response string.
        output_schema: Expected JSON Schema, if any.

    Returns:
        Parsed dict output.

    Raises:
        ValueError: If output_schema is set but response is not valid JSON.
    """
    if output_schema:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(
                f"LLM returned non-JSON response but output_schema was requested: "
                f"{content[:200]}"
            )

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"response": content}
