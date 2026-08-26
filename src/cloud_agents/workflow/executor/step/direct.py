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
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import ModelHTTPError, UserError
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import ModelRequestParameters, OutputObjectDefinition

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


def _supports_native_output(output_schema: dict[str, Any]) -> bool:
    """Whether output_schema's root shape is safe to send via native structured output.

    output_schema is user-authored workflow YAML, not internally
    guaranteed to be an object-rooted JSON Schema. OpenAI's Structured
    Outputs (what output_mode="native" maps to) requires an object root
    -- a top-level array or anyOf/oneOf/allOf union can be rejected by
    the provider. That rejection isn't a pydantic-ai UserError, so it
    wouldn't be caught by the existing native-mode fallback (which
    intentionally only retries on UserError and lets genuine API errors
    propagate, see test_non_user_error_propagates_without_fallback) --
    the schema shape has to be checked before attempting native mode,
    not recovered from after.

    Parameters:
        output_schema: The step's requested JSON Schema.

    Returns:
        True if output_schema has an object root.
    """
    return output_schema.get("type") == "object"


async def _call_llm(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    timeout_seconds: int = 600,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call LLM via pydantic-ai model_request.

    When output_schema is set, first tries native structured output
    (output_mode="native"), which maps to the provider's own JSON-schema
    mode (e.g. OpenAI's response_format) instead of relying on the model
    to follow a plain-text schema hint. Some models reliably wrap
    text-only JSON instructions in markdown fences, which then fails
    _parse_output()'s json.loads(); native mode avoids that at the
    source. Falls back to the plain (schema-hint-only) call if native
    mode isn't supported for this provider/model -- _parse_output()'s
    fence-stripping is the safety net for that fallback path.

    Parameters:
        provider: Provider config (name, model, credentials_secret).
        messages: Chat messages list.
        timeout_seconds: Request timeout.
        output_schema: Expected JSON Schema, if any.

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

    response = None
    if output_schema and _supports_native_output(output_schema):
        try:
            native_params = ModelRequestParameters(
                output_mode="native",
                output_object=OutputObjectDefinition(json_schema=output_schema),
            )
            response = await model_request(
                model_string,
                [request],
                model_settings={"timeout": timeout_seconds},
                model_request_parameters=native_params,
            )
        except UserError as exc:
            logger.warning(
                "Native structured output not supported for %s, falling back to "
                "prompt-based schema hint: %s",
                model_string,
                exc,
            )
        except ModelHTTPError as exc:
            # 400 means the provider rejected the request itself -- for an
            # object-rooted schema that already passed _supports_native_output(),
            # the most likely cause is a native-mode-specific schema
            # constraint (unsupported keywords, draft mismatch, etc.), the
            # same class of "this schema doesn't work in native mode"
            # signal UserError already falls back for. Anything else
            # (401/429/5xx) is an auth/rate-limit/infra failure unrelated
            # to the schema -- re-raise those rather than silently
            # retrying, consistent with the intentional "let real
            # failures propagate" design (see
            # test_5xx_model_http_error_propagates_without_fallback).
            if exc.status_code != 400:
                raise
            logger.warning(
                "Native structured output rejected (400) for %s, falling back to "
                "prompt-based schema hint: %s",
                model_string,
                exc,
            )

    if response is None:
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


def _has_conversation_context(context: dict[str, Any]) -> bool:
    """Detect whether context contains conversation messages.

    Conversation context entries have output.messages as a list of dicts.

    Parameters:
        context: Step context dict.

    Returns:
        True if any context entry has conversation message structure.
    """
    for value in context.values():
        if not isinstance(value, dict):
            continue
        output = value.get("output")
        if isinstance(output, dict) and isinstance(output.get("messages"), list):
            return True
    return False


def _build_message_history(context: dict[str, Any]) -> list[ModelMessage]:
    """Convert prior turn messages in context to pydantic-ai message_history.

    Iterates context keys in sorted order (turn-0, turn-1, ...) and converts
    each conversation message to the appropriate ModelRequest or ModelResponse.

    Parameters:
        context: Step context dict with conversation turn entries.

    Returns:
        List of ModelMessage objects for pydantic-ai message_history.
    """
    history: list[ModelMessage] = []
    tool_call_counter = 0

    def _turn_sort_key(k: str) -> int:
        if k.startswith("turn-"):
            try:
                return int(k.split("-", 1)[1])
            except (ValueError, IndexError):
                pass
        return float("inf")

    for key in sorted(context.keys(), key=_turn_sort_key):
        turn = context[key]
        if not isinstance(turn, dict):
            continue
        output = turn.get("output", {})
        messages = output.get("messages") if isinstance(output, dict) else None
        if not messages:
            continue
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                history.append(ModelRequest.user_text_prompt(content))
            elif role == "assistant":
                history.append(ModelResponse(parts=[TextPart(content=content)]))
            elif role == "tool_call":
                metadata = msg.get("metadata", {})
                tool_name = metadata.get("tool_name", "")
                args = metadata.get("args", {})
                tool_call_id = metadata.get("tool_call_id", f"call_{tool_name}_{tool_call_counter}")
                tool_call_counter += 1
                tool_call_part = ToolCallPart(
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=tool_call_id,
                )
                # Consecutive tool_calls are parts of the same ModelResponse
                if history and isinstance(history[-1], ModelResponse) and any(
                    isinstance(p, ToolCallPart) for p in history[-1].parts
                ):
                    history[-1].parts.append(tool_call_part)
                else:
                    history.append(ModelResponse(parts=[tool_call_part]))
            elif role == "tool_result":
                metadata = msg.get("metadata", {})
                tool_name = metadata.get("tool_name", "")
                tool_call_id = metadata.get("tool_call_id", f"call_{tool_name}_{tool_call_counter}")
                tool_call_counter += 1
                return_part = ToolReturnPart(
                    tool_name=tool_name,
                    content=content if isinstance(content, str) else json.dumps(content),
                    tool_call_id=tool_call_id,
                )
                # Consecutive tool_results grouped in same ModelRequest
                if history and isinstance(history[-1], ModelRequest) and any(
                    isinstance(p, ToolReturnPart) for p in history[-1].parts
                ):
                    history[-1].parts.append(return_part)
                else:
                    history.append(ModelRequest(parts=[return_part]))
    return history


class DirectExecutor(StepExecutor):
    """Execute steps in-process via pydantic-ai.

    This is the spawn: none implementation. It sends a single chat
    completion request and returns the result. Supports structured
    output via output_schema (JSON mode).
    """

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute a step as a single LLM call or pydantic-ai Agent loop.

        When the step has tools, MCP servers, skills, or conversation
        context, creates a pydantic-ai Agent with tools and message_history.
        Otherwise falls back to the simpler model_request path.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with LLM response, token counts, and transcript.
        """
        start_ms = time.monotonic_ns() // 1_000_000

        try:
            skills_cap = get_skills_capability()
            has_conversation = _has_conversation_context(step_input.context)
            if step_input.tools or step_input.mcp_servers or skills_cap or has_conversation:
                return await self._run_with_agent(step_input, start_ms, skills_cap=skills_cap)
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

        When the step has tools, MCP servers, or conversation context,
        uses Agent.run_stream() for true token-by-token streaming.
        Otherwise falls back to the default implementation which calls
        run() and yields a single complete event.

        Parameters:
            step_input: Step execution input.

        Yields:
            StreamEvent instances (token deltas followed by complete/error).
        """
        has_conversation = _has_conversation_context(step_input.context)
        if not (step_input.tools or step_input.mcp_servers or has_conversation):
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

            # Build message_history from conversation context
            message_history = _build_message_history(step_input.context)

            # When using message_history, don't flatten context into the prompt —
            # the prior turns are already structured messages in message_history.
            if message_history:
                user_prompt = step_input.prompt
                if step_input.output_schema:
                    schema_str = json.dumps(step_input.output_schema, indent=2)
                    user_prompt += f"\n\nRespond with JSON matching this schema:\n{schema_str}"
            else:
                user_prompt = _build_user_prompt(step_input)

            async with contextlib.AsyncExitStack() as stack:
                active_toolsets = []
                for ts in mcp_toolsets:
                    active_ts = await stack.enter_async_context(ts)
                    active_toolsets.append(active_ts)

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

                async with agent.run_stream(
                    user_prompt,
                    message_history=message_history if message_history else None,
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

    async def _run_with_agent(
        self, step_input: StepInput, start_ms: int, skills_cap: Any = None
    ) -> StepResult:
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

        # Build message_history from conversation context
        message_history = _build_message_history(step_input.context)

        # When using message_history, don't flatten context into the prompt —
        # the prior turns are already structured messages in message_history.
        if message_history:
            user_prompt = step_input.prompt
            if step_input.output_schema:
                schema_str = json.dumps(step_input.output_schema, indent=2)
                user_prompt += f"\n\nRespond with JSON matching this schema:\n{schema_str}"
        else:
            user_prompt = _build_user_prompt(step_input)

        # Use AsyncExitStack for proper MCPToolset lifecycle management
        async with contextlib.AsyncExitStack() as stack:
            active_toolsets = []
            for ts in mcp_toolsets:
                active_ts = await stack.enter_async_context(ts)
                active_toolsets.append(active_ts)

            # Build capabilities
            capabilities = []
            if skills_cap is None:
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
                message_history=message_history if message_history else None,
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
            output_schema=step_input.output_schema,
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


_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _strip_markdown_fence(content: str) -> str:
    """Strip a wrapping ```json ... ``` / ``` ... ``` code fence, if present.

    Models asked (via plain prompt text, not a provider-native JSON mode)
    to return JSON commonly wrap it in a markdown fence anyway. This is a
    universal safety net independent of _call_llm()'s native-structured-output
    attempt, covering the Agent-based paths (_run_with_agent, run_stream)
    which don't go through that native-mode logic.

    Parameters:
        content: Raw LLM response text.

    Returns:
        Fence-stripped content, or the original string if no fence is found.
    """
    match = _MARKDOWN_FENCE_RE.match(content.strip())
    return match.group(1).strip() if match else content


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
            return json.loads(_strip_markdown_fence(content))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM returned non-JSON response but output_schema was requested: {content[:200]}"
            ) from exc

    try:
        return json.loads(_strip_markdown_fence(content))
    except json.JSONDecodeError:
        return {"response": content}
