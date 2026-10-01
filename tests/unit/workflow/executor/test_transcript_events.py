"""Tests for canonical transcript event reconstruction from pydantic-ai runs.

The canonical per-event transcript contract (``tool_call`` / ``tool_result``
/ ``thinking`` / ``result`` / ``error`` events with ``ts`` / ``type`` /
``data`` keys) is defined by the sandbox agent's EventLogger
(lightspeed-agentic-sandbox src/lightspeed_agentic/logging.py). These tests
pin the executor-side reconstruction from pydantic-ai message history to
that contract, so spawn:none/local transcripts carry the same event types
and data keys as spawn:ephemeral (issue: transcript parity across spawn
modes).
"""

# pylint: disable=protected-access

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel

from cloud_agents.workflow.executor.step.transcript_events import (
    MAX_EVENT_FIELD_LENGTH,
    error_transcript_event,
    transcript_events_from_messages,
)


def _history_with_tool_use() -> list[Any]:
    """Message history of one tool-calling agent run (prompt -> call -> result -> text)."""
    return [
        ModelRequest(parts=[UserPromptPart(content="Inspect the cluster")]),
        ModelResponse(
            parts=[
                ThinkingPart(content="Let me check"),
                ToolCallPart(
                    tool_name="kubectl_get", args={"resource": "pods"}, tool_call_id="call_1"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="kubectl_get", content="pod-list", tool_call_id="call_1"),
            ]
        ),
        ModelResponse(parts=[TextPart(content="All good")]),
    ]


class TestTranscriptEventsFromMessages:
    """Reconstruct canonical events from a pydantic-ai message history."""

    def test_event_types_and_order(self) -> None:
        """A tool-calling run yields thinking, tool_call, tool_result, result in order."""
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
        )

        assert [e["type"] for e in events] == [
            "thinking",
            "tool_call",
            "tool_result",
            "result",
        ]

    def test_event_shape_ts_type_data(self) -> None:
        """Every event is canonical: ts + type + data, nothing flat."""
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
        )

        for event in events:
            assert set(event.keys()) == {"ts", "type", "data"}
            assert isinstance(event["ts"], str) and event["ts"]
            assert isinstance(event["data"], dict)

    def test_data_keys_match_sandbox_contract(self) -> None:
        """data keys mirror the sandbox EventLogger exactly.

        thinking: {text}; tool_call: {name, input}; tool_result: {output};
        result: {text, cost_usd, input_tokens, output_tokens}.
        """
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
        )
        thinking, tool_call, tool_result, result = events

        assert set(thinking["data"].keys()) == {"text"}
        assert thinking["data"]["text"] == "Let me check"

        assert set(tool_call["data"].keys()) == {"name", "input"}
        assert tool_call["data"]["name"] == "kubectl_get"
        assert json.loads(tool_call["data"]["input"]) == {"resource": "pods"}

        assert set(tool_result["data"].keys()) == {"output"}
        assert tool_result["data"]["output"] == "pod-list"

        assert set(result["data"].keys()) == {
            "text",
            "cost_usd",
            "input_tokens",
            "output_tokens",
        }
        assert result["data"]["text"] == "All good"
        assert result["data"]["input_tokens"] == 11
        assert result["data"]["output_tokens"] == 7

    def test_cost_usd_is_none_documented_gap(self) -> None:
        """cost_usd is None for reconstructed runs -- no per-turn cost source.

        pydantic-ai exposes only aggregate usage, so (unlike ephemeral's
        per-turn result events with real cost) cost stays unknown rather
        than being faked as 0.
        """
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
        )
        assert events[-1]["data"]["cost_usd"] is None

    def test_single_result_event_with_aggregate_usage(self) -> None:
        """Exactly one result event per run, carrying aggregate token counts."""
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
        )
        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) == 1

    def test_no_tool_run_yields_only_result(self) -> None:
        """A plain prompt->response run yields a single result event."""
        history = [
            ModelRequest(parts=[UserPromptPart(content="Hi")]),
            ModelResponse(parts=[TextPart(content="Hello")]),
        ]
        events = transcript_events_from_messages(
            history, output_text="Hello", input_tokens=3, output_tokens=2
        )
        assert [e["type"] for e in events] == ["result"]

    def test_string_args_kept_verbatim(self) -> None:
        """ToolCallPart.args already a string is used as-is, not double-encoded."""
        history = [
            ModelResponse(parts=[ToolCallPart(tool_name="t", args='{"k": 1}')]),
            ModelResponse(parts=[TextPart(content="done")]),
        ]
        events = transcript_events_from_messages(
            history, output_text="done", input_tokens=1, output_tokens=1
        )
        assert events[0]["data"]["input"] == '{"k": 1}'

    def test_non_string_tool_content_serialized(self) -> None:
        """dict/list tool return content is JSON-serialized, other types str()'d."""
        history = [
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="t", content={"a": [1]}, tool_call_id="c"),
                    ToolReturnPart(tool_name="u", content=42, tool_call_id="d"),
                ]
            ),
            ModelResponse(parts=[TextPart(content="done")]),
        ]
        events = transcript_events_from_messages(
            history, output_text="done", input_tokens=1, output_tokens=1
        )
        assert json.loads(events[0]["data"]["output"]) == {"a": [1]}
        assert events[1]["data"]["output"] == "42"

    def test_field_truncation_at_sandbox_limits(self) -> None:
        """thinking text / tool input / tool output truncate at 2000 chars."""
        long_text = "x" * (MAX_EVENT_FIELD_LENGTH + 10)
        history = [
            ModelResponse(
                parts=[
                    ThinkingPart(content=long_text),
                    ToolCallPart(tool_name="t", args={"big": long_text}),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="t", content=long_text, tool_call_id="c")]
            ),
            ModelResponse(parts=[TextPart(content="done")]),
        ]
        events = transcript_events_from_messages(
            history, output_text=long_text, input_tokens=1, output_tokens=1
        )
        thinking, tool_call, tool_result, result = events
        assert len(thinking["data"]["text"]) == MAX_EVENT_FIELD_LENGTH
        # Truncation applies after JSON stringification, so the whole
        # input string is capped at the limit while keeping valid-ish prefix.
        assert len(tool_call["data"]["input"]) == MAX_EVENT_FIELD_LENGTH
        assert tool_call["data"]["input"].startswith('{"big": "')
        assert len(tool_result["data"]["output"]) == MAX_EVENT_FIELD_LENGTH
        assert len(result["data"]["text"]) == len(
            long_text
        )  # result text is not truncated (EventLogger parity)

    def test_retry_prompt_part_maps_to_tool_result(self) -> None:
        """A tool retry/error prompt surfaces as a tool_result event."""
        history = [
            ModelRequest(parts=[RetryPromptPart(tool_name="t", tool_call_id="c", content="boom")]),
            ModelResponse(parts=[TextPart(content="recovered")]),
        ]
        events = transcript_events_from_messages(
            history, output_text="recovered", input_tokens=1, output_tokens=1
        )
        assert events[0]["type"] == "tool_result"
        assert "boom" in events[0]["data"]["output"]

    def test_system_prompt_parts_skipped(self) -> None:
        """System prompts are not transcript events."""
        history = [
            ModelRequest(parts=[SystemPromptPart(content="You are terse")]),
            ModelRequest(parts=[UserPromptPart(content="Hi")]),
            ModelResponse(parts=[TextPart(content="Hello")]),
        ]
        events = transcript_events_from_messages(
            history, output_text="Hello", input_tokens=3, output_tokens=2
        )
        assert [e["type"] for e in events] == ["result"]

    def test_explicit_ts_used_verbatim(self) -> None:
        """A caller-provided ts (e.g. run completion time) stamps every event."""
        events = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
            ts="2026-09-30T00:00:00+00:00",
        )
        assert all(e["ts"] == "2026-09-30T00:00:00+00:00" for e in events)

    def test_ts_defaults_to_now_iso_utc(self) -> None:
        """Without ts, events are stamped with the current UTC time in ISO format."""
        events = transcript_events_from_messages(
            _history_with_tool_use(), output_text="x", input_tokens=1, output_tokens=1
        )
        assert all(e["ts"].startswith("20") for e in events)


class TestErrorTranscriptEvent:
    """error events for failed runs (parity with EventLogger.log_error)."""

    def test_error_event_shape(self) -> None:
        """error events carry a single message key under data."""
        event = error_transcript_event("LLM boom")
        assert event["type"] == "error"
        assert set(event.keys()) == {"ts", "type", "data"}
        assert event["data"] == {"message": "LLM boom"}
        assert isinstance(event["ts"], str) and event["ts"]

    def test_error_event_accepts_explicit_ts(self) -> None:
        """An explicit ts is honored."""
        event = error_transcript_event("boom", ts="2026-09-30T00:00:00+00:00")
        assert event["ts"] == "2026-09-30T00:00:00+00:00"


class TestNormalizePassthrough:
    """Reconstructed events must survive normalize_transcript_events unchanged."""

    def test_events_are_already_canonical(self) -> None:
        """normalize_transcript_events passes reconstructed events through as-is."""
        from cloud_agents.workflow.core.models import (  # pylint: disable=import-outside-toplevel
            normalize_transcript_events,
        )

        raw = transcript_events_from_messages(
            _history_with_tool_use(),
            output_text="All good",
            input_tokens=11,
            output_tokens=7,
            ts="2026-09-30T00:00:00+00:00",
        )
        normalized = normalize_transcript_events(raw)
        assert [e.model_dump() for e in normalized] == raw


class TestEphemeralParity:
    """Reconstructed transcripts expose the same event-type vocabulary as ephemeral."""

    @pytest.mark.parametrize(
        "event_type",
        ["tool_call", "tool_result", "thinking", "result", "error"],
    )
    def test_every_canonical_type_reachable(self, event_type: str) -> None:
        """Each canonical event type is producible by the reconstruction."""
        if event_type == "error":
            assert error_transcript_event("x")["type"] == "error"
            return
        events = transcript_events_from_messages(
            _history_with_tool_use(), output_text="x", input_tokens=1, output_tokens=1
        )
        assert event_type in [e["type"] for e in events]

    def test_types_subset_of_canonical_literal(self) -> None:
        """No reconstruction ever emits a type outside the canonical Literal."""
        events = transcript_events_from_messages(
            _history_with_tool_use(), output_text="x", input_tokens=1, output_tokens=1
        )
        canonical = {"tool_call", "tool_result", "thinking", "result", "error"}
        assert {e["type"] for e in events} <= canonical


class TestDirectExecutorEmission:
    """DirectExecutor (spawn: none) emits canonical events end-to-end.

    Runs the real pydantic-ai agent loop (a genuinely registered step
    tool called by TestModel) through DirectExecutor.run -- no Agent
    mocks -- so the emission path, not just the converter, is covered.
    """

    TOOL_NAME = "parity_probe_tool"

    @pytest.mark.asyncio
    async def test_tool_run_emits_canonical_events(self, mocker: Any) -> None:
        """A real tool-calling agent run produces tool_call/tool_result/result."""
        from cloud_agents.workflow.executor.step import tools as tools_module
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        def _probe() -> str:
            return "probe-ok"

        tools_module._REGISTRY[self.TOOL_NAME] = tools_module.ToolDefinition(
            name=self.TOOL_NAME, func=_probe, description="parity probe"
        )
        try:
            mocker.patch("cloud_agents.workflow.executor.step.direct.ensure_credentials_env")
            mocker.patch(
                "cloud_agents.workflow.executor.step.direct.to_model_string",
                return_value=TestModel(),
            )

            result = await DirectExecutor().run(
                StepInput(
                    prompt="Probe",
                    provider={"name": "openai", "model": "gpt-4o"},
                    tools=[self.TOOL_NAME],
                    step_name="parity-step",
                )
            )

            assert result.status == "completed"
            assert [e["type"] for e in result.transcript] == [
                "tool_call",
                "tool_result",
                "result",
            ]
            tool_call, tool_result, final = result.transcript
            assert set(tool_call["data"]) == {"name", "input"}
            assert tool_call["data"]["name"] == self.TOOL_NAME
            assert json.loads(tool_call["data"]["input"]) == {}
            assert set(tool_result["data"]) == {"output"}
            assert tool_result["data"]["output"] == "probe-ok"
            assert set(final["data"]) == {"text", "cost_usd", "input_tokens", "output_tokens"}
            assert final["data"]["cost_usd"] is None
        finally:
            tools_module._REGISTRY.pop(self.TOOL_NAME, None)

    @pytest.mark.asyncio
    async def test_failed_run_emits_error_event(self, mocker: Any) -> None:
        """A failing run yields a transcript with a single error event."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.direct.ensure_credentials_env",
            side_effect=RuntimeError("credentials blew up"),
        )

        result = await DirectExecutor().run(
            StepInput(
                prompt="Probe",
                provider={"name": "openai", "model": "gpt-4o"},
                step_name="parity-step",
            )
        )

        assert result.status == "failed"
        assert [e["type"] for e in result.transcript] == ["error"]
        assert result.transcript[0]["data"] == {"message": "credentials blew up"}


class TestSubprocessChildEmission:
    """subprocess_child (spawn: local) emits the same canonical events.

    Runs the real pydantic-ai agent loop inside the child entrypoint
    (TestModel calling a registered tool) through main()'s stdin/stdout
    protocol -- so the subprocess path's emission, not just the shared
    converter, is covered.
    """

    TOOL_NAME = "parity_probe_tool_local"

    def test_tool_run_emits_canonical_events(self, mocker: Any) -> None:
        """A child tool-calling run returns tool_call/tool_result/result events."""
        from cloud_agents.workflow.executor.step import subprocess_child
        from cloud_agents.workflow.executor.step.tools import (
            clear_tools,
            register_tool,
        )

        def _probe() -> str:
            return "probe-local-ok"

        register_tool(self.TOOL_NAME, _probe)
        try:
            mocker.patch(
                "cloud_agents.workflow.executor.step.subprocess_child.ensure_credentials_env"
            )
            mocker.patch(
                "cloud_agents.workflow.executor.step.subprocess_child.to_model_string",
                return_value=TestModel(),
            )

            input_data = {
                "prompt": "Probe",
                "provider": {"name": "openai", "model": "gpt-4o"},
                "context": {},
                "tools": [self.TOOL_NAME],
            }
            stdin_mock = StringIO(json.dumps(input_data))
            stdout_mock = StringIO()
            mocker.patch("sys.stdin", stdin_mock)
            mocker.patch("sys.stdout", stdout_mock)

            subprocess_child.main()

            stdout_mock.seek(0)
            result = json.loads(stdout_mock.read())
        finally:
            clear_tools()

        assert result["status"] == "completed"
        assert [e["type"] for e in result["transcript"]] == [
            "tool_call",
            "tool_result",
            "result",
        ]
        tool_call, tool_result, final = result["transcript"]
        assert tool_call["data"]["name"] == self.TOOL_NAME
        assert json.loads(tool_call["data"]["input"]) == {}
        assert tool_result["data"]["output"] == "probe-local-ok"
        assert set(final["data"]) == {"text", "cost_usd", "input_tokens", "output_tokens"}

    def test_failed_child_run_emits_error_event(self, mocker: Any) -> None:
        """A crashing child returns a transcript with a single error event."""
        from cloud_agents.workflow.executor.step import subprocess_child

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_child._run",
            side_effect=RuntimeError("child blew up"),
        )

        stdin_mock = StringIO(json.dumps({"prompt": "x", "provider": {}}))
        stdout_mock = StringIO()
        mocker.patch("sys.stdin", stdin_mock)
        mocker.patch("sys.stdout", stdout_mock)

        subprocess_child.main()

        stdout_mock.seek(0)
        result = json.loads(stdout_mock.read())

        assert result["status"] == "failed"
        assert [e["type"] for e in result["transcript"]] == ["error"]
        assert result["transcript"][0]["data"] == {"message": "child blew up"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["agent", "model_request"])
@pytest.mark.parametrize("content", ["not JSON", None, '{"answer": "ok"}'])
async def test_subprocess_parse_failure_emits_error(mocker: Any, path: str, content: Any) -> None:
    """Both child execution paths retain usage and expose parsing failures."""
    from types import SimpleNamespace

    from cloud_agents.workflow.executor.step import subprocess_child

    mocker.patch.object(subprocess_child, "ensure_credentials_env")
    mocker.patch.object(subprocess_child, "to_model_string", return_value=TestModel())
    usage = SimpleNamespace(input_tokens=11, output_tokens=7)
    if path == "agent":
        agent = mocker.patch.object(subprocess_child, "Agent").return_value
        agent.run = mocker.AsyncMock(
            return_value=SimpleNamespace(
                output=content,
                usage=usage,
                new_messages=lambda: [],
            )
        )
    else:
        mocker.patch.object(
            subprocess_child,
            "model_request",
            new=mocker.AsyncMock(
                return_value=SimpleNamespace(text=content, usage=usage),
            ),
        )
    input_data = {
        "prompt": "Return JSON",
        "provider": {"name": "openai", "model": "gpt-4o"},
        "output_schema": {"type": "object"},
    }
    if path == "agent":
        result = await subprocess_child._run_with_agent(input_data, [])
    else:
        result = await subprocess_child._run_model_request(input_data)
    assert result["input_tokens"] == 11
    assert result["output_tokens"] == 7
    if content == '{"answer": "ok"}':
        assert result["status"] == "completed"
        assert [e["type"] for e in result["transcript"]] == ["result"]
    else:
        assert result["status"] == "failed"
        assert [e["type"] for e in result["transcript"]] == ["result", "error"]
        assert result["transcript"][-1]["data"] == {"message": result["error"]}
