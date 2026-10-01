"""Canonical transcript event reconstruction from pydantic-ai runs.

The per-event transcript contract for workflow agent steps is defined by
the sandbox agent's ``EventLogger``
(lightspeed-agentic-sandbox src/lightspeed_agentic/logging.py): events
shaped ``{"ts": <iso>, "type": <str>, "data": {...}}`` with types
``tool_call`` / ``tool_result`` / ``thinking`` / ``result`` / ``error``.

Historically only the ephemeral (sandbox) spawn mode produced that shape;
``DirectExecutor`` (spawn: none) emitted a single flat summary entry and
``SubprocessExecutor`` (spawn: local) a ``[{role: user}, {role:
assistant}]`` message pair, so the same logical run was not comparable
across spawn modes. This module reconstructs the canonical events from
the pydantic-ai message history that both in-process and subprocess
executors already hold, giving every spawn mode the same event types and
data keys.

Documented gaps versus ephemeral (see docs/workflow-transcript-contract.md):

- pydantic-ai exposes only aggregate ``usage`` on a run result, so the
  reconstruction emits exactly ONE ``result`` event carrying aggregate
  ``input_tokens`` / ``output_tokens`` -- not one per agent turn.
- ``cost_usd`` is ``None`` (unknown), never faked as ``0``: consumers
  summing result-event usage (e.g. ``_sum_result_event_usage``) treat
  non-numeric values as zero.
- ``ts`` carries the run completion timestamp for every event; event
  order is significant, timestamps are not (matches normalize semantics).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Optional

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)

# Mirrors EventLogger's MAX_EVENT_TOOL_INPUT / MAX_EVENT_TOOL_OUTPUT /
# MAX_EVENT_THINKING (2000) -- the sandbox truncates these event fields, so
# the reconstruction must too, or parity would differ on large payloads.
MAX_EVENT_FIELD_LENGTH = 2000


def _now_iso() -> str:
    """Current UTC time in ISO-8601, matching EventLogger's ts format."""
    return datetime.now(tz=UTC).isoformat()


def _stringify(value: Any) -> str:
    """Render a tool argument/return value as a string.

    Parameters:
        value: The raw value -- JSON-compatible containers are dumped,
            scalars fall back to ``str()``.

    Returns:
        String form of the value.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _result_event(
    *,
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    ts: str,
) -> dict[str, Any]:
    """Build the run-level canonical ``result`` event.

    Parameters:
        output_text: Final agent output text.
        input_tokens: Aggregate input tokens for the whole run.
        output_tokens: Aggregate output tokens for the whole run.
        ts: Event timestamp.

    Returns:
        Canonical result event dict.
    """
    return {
        "ts": ts,
        "type": "result",
        "data": {
            "text": output_text,
            "cost_usd": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def result_transcript_event(
    *,
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    ts: Optional[str] = None,
) -> dict[str, Any]:
    """Build a standalone canonical ``result`` event (no tool history).

    For plain model-request runs (no tools) there is no tool-loop message
    history to reconstruct -- the run's transcript is exactly one
    ``result`` event with the aggregate usage, same as a tool-loop run
    whose history contained no event-bearing parts.

    Parameters:
        output_text: Final output text of the run.
        input_tokens: Aggregate input token count.
        output_tokens: Aggregate output token count.
        ts: Event timestamp. Defaults to now, UTC.

    Returns:
        Canonical result event dict.
    """
    return _result_event(
        output_text=output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        ts=ts if ts is not None else _now_iso(),
    )


def output_text_of(content: Any) -> str:
    """Render a run's final output as the result event's text.

    Parameters:
        content: The run's output (``result.output`` / response text) --
            usually ``str``; containers are JSON-dumped, other non-str
            values fall back to ``str()``.

    Returns:
        String form of the output.
    """
    return _stringify(content)


def transcript_events_from_messages(
    messages: list[ModelMessage],
    *,
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    ts: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Reconstruct canonical transcript events from pydantic-ai message history.

    Walks the run's message history in order and emits one canonical
    event per ``ThinkingPart`` / ``ToolCallPart`` / ``ToolReturnPart`` /
    ``RetryPromptPart``, then a single run-level ``result`` event with
    aggregate usage. User/system prompts and intermediate text parts are
    not transcript events (the final text lives in the ``result`` event).

    Parameters:
        messages: This run's new messages (``result.new_messages()``) --
            excluding any prior-turn ``message_history`` so multi-turn
            runs do not re-record earlier turns' tool calls.
        output_text: Final output text of the run.
        input_tokens: Aggregate input token count.
        output_tokens: Aggregate output token count.
        ts: Timestamp stamped on every event (e.g. run completion time).
            Defaults to now, UTC.

    Returns:
        Canonical transcript event dicts (``{"ts", "type", "data"}``).
    """
    stamp = ts if ts is not None else _now_iso()
    events: list[dict[str, Any]] = []

    for message in messages:
        parts: list[Any]
        if isinstance(message, ModelResponse):
            parts = message.parts
        elif isinstance(message, ModelRequest):
            parts = message.parts
        else:  # pragma: no cover - pydantic-ai only defines those two
            continue

        for part in parts:
            if isinstance(part, ThinkingPart):
                text = part.content.strip()[:MAX_EVENT_FIELD_LENGTH]
                if text:
                    events.append({"ts": stamp, "type": "thinking", "data": {"text": text}})
            elif isinstance(part, ToolCallPart):
                events.append(
                    {
                        "ts": stamp,
                        "type": "tool_call",
                        "data": {
                            "name": part.tool_name,
                            "input": _stringify(part.args)[:MAX_EVENT_FIELD_LENGTH],
                        },
                    }
                )
            elif isinstance(part, ToolReturnPart):
                events.append(
                    {
                        "ts": stamp,
                        "type": "tool_result",
                        "data": {"output": _stringify(part.content)[:MAX_EVENT_FIELD_LENGTH]},
                    }
                )
            elif isinstance(part, RetryPromptPart):
                events.append(
                    {
                        "ts": stamp,
                        "type": "tool_result",
                        "data": {"output": _stringify(part.content)[:MAX_EVENT_FIELD_LENGTH]},
                    }
                )
            # UserPromptPart / SystemPromptPart / TextPart: not events.

    events.append(
        _result_event(
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ts=stamp,
        )
    )
    return events


def error_transcript_event(message: str, *, ts: Optional[str] = None) -> dict[str, Any]:
    """Build a canonical ``error`` event for a failed run.

    Parameters:
        message: Failure description (parity with EventLogger.log_error,
            not truncated).
        ts: Event timestamp. Defaults to now, UTC.

    Returns:
        Canonical error event dict.
    """
    return {
        "ts": ts if ts is not None else _now_iso(),
        "type": "error",
        "data": {"message": message},
    }
