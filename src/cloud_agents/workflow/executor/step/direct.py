"""DirectExecutor — spawn: none LLM-only step executor.

Executes a single LLM call with no tools or agent loop.
Uses the OpenAI-compatible chat completions API via httpx.

No temporalio imports.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from cloud_agents.workflow.executor.step.base import StepExecutor, StepInput, StepResult

logger = logging.getLogger(__name__)

_PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
}

_UNSUPPORTED_NATIVE_PROVIDERS: set[str] = {"anthropic", "azure"}

_PROVIDER_ENV_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def _resolve_api_key(provider: dict[str, Any]) -> str | None:
    """Resolve LLM API key from provider config and environment.

    Parameters:
        provider: Provider configuration dict.

    Returns:
        API key string or None if not found.
    """
    cred_secret = provider.get("credentials_secret", "")
    if cred_secret:
        env_key = cred_secret.upper().replace("-", "_")
        return os.environ.get(env_key) or os.environ.get(cred_secret)

    provider_name = provider.get("name", "openai")
    default_key = _PROVIDER_ENV_KEYS.get(provider_name, "")
    if default_key:
        return os.environ.get(default_key)
    return None


def _resolve_base_url(provider: dict[str, Any]) -> str:
    """Resolve LLM API base URL from provider config.

    Parameters:
        provider: Provider configuration dict.

    Returns:
        Base URL string for the API endpoint.

    Raises:
        ValueError: If the provider requires a non-OpenAI API and no base_url is set.
    """
    if base_url := provider.get("base_url"):
        return base_url
    provider_name = provider.get("name", "openai")
    if provider_name in _UNSUPPORTED_NATIVE_PROVIDERS:
        raise ValueError(
            f"Provider '{provider_name}' uses a non-OpenAI-compatible API. "
            "Set 'base_url' to an OpenAI-compatible proxy, or use spawn: ephemeral."
        )
    return _PROVIDER_BASE_URLS.get(provider_name, _PROVIDER_BASE_URLS["openai"])


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
            if isinstance(value, dict) and value.get("output"):
                context_parts.append(
                    f"Previous step '{key}': {json.dumps(value['output'], indent=2)}"
                )
        if context_parts:
            context_block = "\n\n".join(context_parts)
            user_content = f"{user_content}\n\n--- Prior step outputs ---\n{context_block}"

    if step_input.output_schema:
        schema_str = json.dumps(step_input.output_schema, indent=2)
        user_content = (
            f"{user_content}\n\nRespond with JSON matching this schema:\n{schema_str}"
        )

    messages.append({"role": "user", "content": user_content})
    return messages


async def _call_llm(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    output_schema: dict[str, Any] | None = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Call LLM via OpenAI-compatible chat completions API.

    Parameters:
        provider: Provider config (name, model, credentials_secret).
        messages: Chat messages list.
        output_schema: Optional JSON Schema for structured output.
        timeout_seconds: Request timeout.

    Returns:
        Dict with content, input_tokens, output_tokens.

    Raises:
        ValueError: If API key cannot be resolved.
        httpx.HTTPStatusError: If API returns error status.
    """
    api_key = _resolve_api_key(provider)
    if not api_key:
        raise ValueError(
            f"Could not resolve API key for provider '{provider.get('name', 'unknown')}'. "
            f"Set the credential environment variable (e.g. OPENAI_API_KEY)."
        )

    base_url = _resolve_base_url(provider)
    model = provider.get("model", "gpt-4o")

    request_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }

    if output_schema:
        request_body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage", {})

    return {
        "content": content,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
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

            output = _parse_output(content)

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
            logger.error("DirectExecutor credential error for step '%s': %s", step_input.step_name, exc)
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


def _parse_output(content: str) -> dict[str, Any]:
    """Parse LLM response content into structured output.

    Parameters:
        content: Raw LLM response string.

    Returns:
        Parsed dict output, or {"response": content} if not valid JSON.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"response": content}
    if isinstance(parsed, dict):
        return parsed
    return {"response": content}
