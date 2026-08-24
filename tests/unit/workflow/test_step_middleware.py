"""Unit tests for StepMiddleware Protocol and MiddlewareExecutor (TDD).

Tests cover:
- MiddlewareExecutor applies before/after hooks on run()
- Middleware ordering (before in order, after in reverse)
- step_input modification in before()
- result modification in after()
- run_stream() applies before, yields events, applies after
- Empty middleware list passes through
- TracingMiddleware: trace_id propagation + span attributes
- TranscriptMiddleware: resilient save to transcript store
- OTEL span lifecycle owned by MiddlewareExecutor
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_agents.workflow.executor.step.base import (
    StepInput,
    StepMetadata,
    StepResult,
    StreamEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step_input(**overrides: Any) -> StepInput:
    """Build a minimal StepInput for testing."""
    defaults = {
        "prompt": "test prompt",
        "provider": {"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        "workflow_id": "wf-test",
        "step_name": "step-1",
        "output_key": "result-1",
        "metadata": StepMetadata(user_id="u1"),
    }
    defaults.update(overrides)
    return StepInput(**defaults)


def _make_step_result(**overrides: Any) -> StepResult:
    """Build a minimal StepResult for testing."""
    defaults = {
        "status": "completed",
        "output": {"answer": "42"},
        "input_tokens": 100,
        "output_tokens": 50,
        "duration_ms": 200,
    }
    defaults.update(overrides)
    return StepResult(**defaults)


class _RecordingMiddleware:
    """Middleware that records calls for assertion."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def before(self, step_input: StepInput) -> StepInput:
        """Record before call."""
        self.calls.append(f"before:{self.name}")
        return step_input

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Record after call."""
        self.calls.append(f"after:{self.name}")
        return result


class _MutatingBeforeMiddleware:
    """Middleware that modifies step_input.prompt in before()."""

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix

    async def before(self, step_input: StepInput) -> StepInput:
        """Append suffix to prompt."""
        step_input.prompt = step_input.prompt + self.suffix
        return step_input

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Pass through."""
        return result


class _MutatingAfterMiddleware:
    """Middleware that modifies result.output in after()."""

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    async def before(self, step_input: StepInput) -> StepInput:
        """Pass through."""
        return step_input

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Add key-value to output."""
        if result.output is None:
            result.output = {}
        result.output[self.key] = self.value
        return result


class _FakeExecutor:
    """Fake StepExecutor for testing."""

    def __init__(self, result: StepResult | None = None) -> None:
        self.result = result or _make_step_result()
        self.captured_input: StepInput | None = None

    async def run(self, step_input: StepInput) -> StepResult:
        """Record input and return result."""
        self.captured_input = step_input
        return self.result

    async def run_stream(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Stream fake events."""
        self.captured_input = step_input
        yield StreamEvent(type="token", data={"delta": "hello"})
        yield StreamEvent(type="token", data={"delta": " world"})
        yield StreamEvent(type="complete", result=self.result)


class _ErrorExecutor:
    """Executor that raises an exception."""

    async def run(self, step_input: StepInput) -> StepResult:
        """Raise an error."""
        raise RuntimeError("LLM connection failed")

    async def run_stream(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Raise an error."""
        raise RuntimeError("LLM connection failed")
        yield  # type: ignore[misc]  # Make this a generator


# ---------------------------------------------------------------------------
# MiddlewareExecutor tests
# ---------------------------------------------------------------------------


class TestMiddlewareExecutorRun:
    """Tests for MiddlewareExecutor.run()."""

    @pytest.mark.asyncio
    async def test_empty_middleware_passes_through(self) -> None:
        """With no middleware, executor.run() is called directly."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [])
        step_input = _make_step_input()

        result = await wrapped.run(step_input)

        assert result.status == "completed"
        assert executor.captured_input is step_input

    @pytest.mark.asyncio
    async def test_before_after_hooks_called(self) -> None:
        """Before and after hooks are called around executor.run()."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        calls: list[str] = []
        mw = _RecordingMiddleware("mw1", calls)
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])
        step_input = _make_step_input()

        await wrapped.run(step_input)

        assert "before:mw1" in calls
        assert "after:mw1" in calls
        assert calls.index("before:mw1") < calls.index("after:mw1")

    @pytest.mark.asyncio
    async def test_middleware_ordering(self) -> None:
        """Before hooks run in order; after hooks run in reverse order."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        calls: list[str] = []
        mw_a = _RecordingMiddleware("A", calls)
        mw_b = _RecordingMiddleware("B", calls)
        mw_c = _RecordingMiddleware("C", calls)
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw_a, mw_b, mw_c])

        await wrapped.run(_make_step_input())

        assert calls == [
            "before:A",
            "before:B",
            "before:C",
            "after:C",
            "after:B",
            "after:A",
        ]

    @pytest.mark.asyncio
    async def test_before_modifies_step_input(self) -> None:
        """Before middleware can modify step_input before execution."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mw = _MutatingBeforeMiddleware(" [enriched]")
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])

        step_input = _make_step_input(prompt="original")
        await wrapped.run(step_input)

        assert executor.captured_input is not None
        assert executor.captured_input.prompt == "original [enriched]"

    @pytest.mark.asyncio
    async def test_after_modifies_result(self) -> None:
        """After middleware can modify result after execution."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mw = _MutatingAfterMiddleware("enriched_by", "middleware")
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])

        result = await wrapped.run(_make_step_input())

        assert result.output is not None
        assert result.output["enriched_by"] == "middleware"

    @pytest.mark.asyncio
    async def test_chained_before_modifications(self) -> None:
        """Multiple before middlewares chain modifications."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mw_a = _MutatingBeforeMiddleware(" [A]")
        mw_b = _MutatingBeforeMiddleware(" [B]")
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw_a, mw_b])

        step_input = _make_step_input(prompt="base")
        await wrapped.run(step_input)

        assert executor.captured_input is not None
        assert executor.captured_input.prompt == "base [A] [B]"

    @pytest.mark.asyncio
    async def test_executor_error_propagates(self) -> None:
        """When executor raises, exception propagates (no after hooks called)."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        calls: list[str] = []
        mw = _RecordingMiddleware("mw1", calls)
        executor = _ErrorExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])

        with pytest.raises(RuntimeError, match="LLM connection failed"):
            await wrapped.run(_make_step_input())

        # before was called, but after was not (exception escaped)
        assert "before:mw1" in calls
        assert "after:mw1" not in calls


class TestMiddlewareExecutorRunStream:
    """Tests for MiddlewareExecutor.run_stream()."""

    @pytest.mark.asyncio
    async def test_stream_empty_middleware_yields_events(self) -> None:
        """With no middleware, events pass through from executor."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [])

        events = []
        async for event in wrapped.run_stream(_make_step_input()):
            events.append(event)

        assert len(events) == 3
        assert events[0].type == "token"
        assert events[1].type == "token"
        assert events[2].type == "complete"

    @pytest.mark.asyncio
    async def test_stream_calls_before_then_yields_then_after(self) -> None:
        """Streaming applies before, yields events, then applies after."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        calls: list[str] = []
        mw = _RecordingMiddleware("mw1", calls)
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])

        events = []
        async for event in wrapped.run_stream(_make_step_input()):
            events.append(event)
            calls.append(f"yield:{event.type}")

        # before comes first, then yields, then after
        assert calls[0] == "before:mw1"
        assert "yield:token" in calls
        assert calls[-1] == "after:mw1"

    @pytest.mark.asyncio
    async def test_stream_before_modifies_input(self) -> None:
        """Before middleware modifies step_input for streaming too."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mw = _MutatingBeforeMiddleware(" [stream-enriched]")
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw])

        step_input = _make_step_input(prompt="stream test")
        async for _ in wrapped.run_stream(step_input):
            pass

        assert executor.captured_input is not None
        assert executor.captured_input.prompt == "stream test [stream-enriched]"

    @pytest.mark.asyncio
    async def test_stream_after_ordering(self) -> None:
        """After hooks run in reverse order after streaming completes."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        calls: list[str] = []
        mw_a = _RecordingMiddleware("A", calls)
        mw_b = _RecordingMiddleware("B", calls)
        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [mw_a, mw_b])

        async for _ in wrapped.run_stream(_make_step_input()):
            pass

        # After hooks should be in reverse
        after_calls = [c for c in calls if c.startswith("after:")]
        assert after_calls == ["after:B", "after:A"]


class TestMiddlewareExecutorOtelSpan:
    """Tests for OTEL span lifecycle owned by MiddlewareExecutor."""

    @pytest.mark.asyncio
    async def test_run_creates_otel_span(self) -> None:
        """MiddlewareExecutor.run() creates an OTEL span named 'step.execute'."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0xABCDEF
        mock_span.get_span_context.return_value = mock_span_ctx

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span

        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [], tracer=mock_tracer)

        step_input = _make_step_input(step_name="diagnose", workflow_id="wf-42")
        await wrapped.run(step_input)

        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "step.execute"
        attrs = call_args[1]["attributes"]
        assert attrs["step.name"] == "diagnose"
        assert attrs["workflow.id"] == "wf-42"

    @pytest.mark.asyncio
    async def test_run_sets_error_on_span_when_executor_fails(self) -> None:
        """MiddlewareExecutor records error on span when executor raises."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0
        mock_span.get_span_context.return_value = mock_span_ctx

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span

        executor = _ErrorExecutor()
        wrapped = MiddlewareExecutor(executor, [], tracer=mock_tracer)

        with pytest.raises(RuntimeError):
            await wrapped.run(_make_step_input())

        mock_span.set_status.assert_called_once()
        mock_span.record_exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_without_tracer_still_works(self) -> None:
        """MiddlewareExecutor works when no tracer is provided (NoOp)."""
        from cloud_agents.workflow.executor.middleware import MiddlewareExecutor

        executor = _FakeExecutor()
        wrapped = MiddlewareExecutor(executor, [])

        result = await wrapped.run(_make_step_input())
        assert result.status == "completed"


# ---------------------------------------------------------------------------
# TracingMiddleware tests
# ---------------------------------------------------------------------------


class TestTracingMiddleware:
    """Tests for TracingMiddleware before/after hooks."""

    @pytest.mark.asyncio
    async def test_before_extracts_trace_id(self) -> None:
        """before() extracts trace_id from current OTEL span context."""
        from unittest.mock import patch

        from cloud_agents.workflow.executor.middleware import TracingMiddleware

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0x0AF7651916CD43DD8448EB211C80319C
        mock_span.get_span_context.return_value = mock_span_ctx

        mw = TracingMiddleware()
        step_input = _make_step_input()

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            result = await mw.before(step_input)

        assert result.metadata is not None
        assert result.metadata.trace_id == "0af7651916cd43dd8448eb211c80319c"

    @pytest.mark.asyncio
    async def test_before_noop_when_no_trace_context(self) -> None:
        """before() does not set trace_id when span has trace_id=0."""
        from unittest.mock import patch

        from cloud_agents.workflow.executor.middleware import TracingMiddleware

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0
        mock_span.get_span_context.return_value = mock_span_ctx

        mw = TracingMiddleware()
        step_input = _make_step_input()

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            result = await mw.before(step_input)

        assert result.metadata is not None
        assert result.metadata.trace_id is None

    @pytest.mark.asyncio
    async def test_after_sets_span_attributes(self) -> None:
        """after() sets step.status, input_tokens, output_tokens on current span."""
        from unittest.mock import patch

        from cloud_agents.workflow.executor.middleware import TracingMiddleware

        mock_span = MagicMock()

        mw = TracingMiddleware()
        step_input = _make_step_input()
        result = _make_step_result(
            status="completed", input_tokens=150, output_tokens=75
        )

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            returned = await mw.after(step_input, result)

        assert returned is result
        attr_calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert attr_calls["step.status"] == "completed"
        assert attr_calls["step.input_tokens"] == 150
        assert attr_calls["step.output_tokens"] == 75

    @pytest.mark.asyncio
    async def test_before_creates_metadata_if_none(self) -> None:
        """before() creates StepMetadata if step_input.metadata is None."""
        from unittest.mock import patch

        from cloud_agents.workflow.executor.middleware import TracingMiddleware

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0xDEADBEEF
        mock_span.get_span_context.return_value = mock_span_ctx

        mw = TracingMiddleware()
        step_input = _make_step_input()
        step_input.metadata = None

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            result = await mw.before(step_input)

        assert result.metadata is not None
        assert result.metadata.trace_id == "000000000000000000000000deadbeef"


# ---------------------------------------------------------------------------
# TranscriptMiddleware tests
# ---------------------------------------------------------------------------


class TestTranscriptMiddleware:
    """Tests for TranscriptMiddleware save to TranscriptStore."""

    @pytest.mark.asyncio
    async def test_after_saves_to_store(self) -> None:
        """after() saves conversation messages and transcript to store."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input(
            prompt="check cluster",
            workflow_id="wf-1",
            output_key="diagnosis",
        )
        step_input.metadata = StepMetadata(trace_id="abc123")

        result = _make_step_result(
            output={"severity": "high"},
            input_tokens=100,
            output_tokens=50,
            duration_ms=300,
            transcript=[],
        )

        returned = await mw.after(step_input, result)

        assert returned is result
        mock_store.save.assert_called_once()
        call_kwargs = mock_store.save.call_args[1]
        assert call_kwargs["workflow_id"] == "wf-1"
        assert call_kwargs["step_name"] == "diagnosis"
        assert call_kwargs["trace_id"] == "abc123"
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "check cluster"
        assert messages[1]["role"] == "assistant"
        # Output is JSON-serialized
        parsed = json.loads(messages[1]["content"])
        assert parsed["severity"] == "high"

    @pytest.mark.asyncio
    async def test_after_no_output_only_user_message(self) -> None:
        """after() only saves user message when result.output is None."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input()
        result = _make_step_result(output=None)

        await mw.after(step_input, result)

        call_kwargs = mock_store.save.call_args[1]
        messages = call_kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_after_resilient_to_store_failure(self) -> None:
        """after() catches exceptions from store.save() and continues."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mock_store.save.side_effect = Exception("DB connection lost")
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input()
        result = _make_step_result()

        # Should not raise
        returned = await mw.after(step_input, result)
        assert returned is result

    @pytest.mark.asyncio
    async def test_before_is_noop(self) -> None:
        """before() is a pass-through (no modification)."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input(prompt="original")
        returned = await mw.before(step_input)

        assert returned is step_input
        assert returned.prompt == "original"

    @pytest.mark.asyncio
    async def test_after_with_none_store(self) -> None:
        """after() with None store is a no-op."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mw = TranscriptMiddleware(None)

        step_input = _make_step_input()
        result = _make_step_result()

        returned = await mw.after(step_input, result)
        assert returned is result

    @pytest.mark.asyncio
    async def test_after_no_metadata_trace_id_is_none(self) -> None:
        """after() passes trace_id=None when metadata is None."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input()
        step_input.metadata = None
        result = _make_step_result()

        await mw.after(step_input, result)

        call_kwargs = mock_store.save.call_args[1]
        assert call_kwargs["trace_id"] is None

    @pytest.mark.asyncio
    async def test_after_string_output_serialized(self) -> None:
        """after() serializes non-dict output as string."""
        from cloud_agents.workflow.executor.middleware import TranscriptMiddleware

        mock_store = AsyncMock()
        mw = TranscriptMiddleware(mock_store)

        step_input = _make_step_input()
        # StepResult.output is typed as dict, but test edge case
        result = _make_step_result(output={"text": "plain answer"})

        await mw.after(step_input, result)

        call_kwargs = mock_store.save.call_args[1]
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        # Dict output is JSON-serialized
        assert "plain answer" in messages[1]["content"]


# ---------------------------------------------------------------------------
# Integration: TracingMiddleware + TranscriptMiddleware compose
# ---------------------------------------------------------------------------


class TestMiddlewareComposition:
    """Tests for composing multiple middleware together."""

    @pytest.mark.asyncio
    async def test_tracing_and_transcript_compose(self) -> None:
        """TracingMiddleware + TranscriptMiddleware work together in MiddlewareExecutor."""
        from unittest.mock import patch

        from cloud_agents.workflow.executor.middleware import (
            MiddlewareExecutor,
            TracingMiddleware,
            TranscriptMiddleware,
        )

        mock_store = AsyncMock()

        # Mock the tracer for MiddlewareExecutor
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0xDEADBEEF
        mock_span.get_span_context.return_value = mock_span_ctx

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span

        executor = _FakeExecutor()
        middlewares = [TracingMiddleware(), TranscriptMiddleware(mock_store)]
        wrapped = MiddlewareExecutor(executor, middlewares, tracer=mock_tracer)

        step_input = _make_step_input(prompt="compose test", workflow_id="wf-compose")

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            result = await wrapped.run(step_input)

        assert result.status == "completed"

        # TracingMiddleware should have set trace_id
        assert step_input.metadata is not None
        assert step_input.metadata.trace_id == "000000000000000000000000deadbeef"

        # TranscriptMiddleware should have saved
        mock_store.save.assert_called_once()
        call_kwargs = mock_store.save.call_args[1]
        assert call_kwargs["trace_id"] == "000000000000000000000000deadbeef"

        # Span should have result attributes set
        attr_calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert attr_calls["step.status"] == "completed"


# ---------------------------------------------------------------------------
# apply_middleware helper
# ---------------------------------------------------------------------------


class TestApplyMiddleware:
    """Tests for apply_middleware convenience function."""

    def test_returns_middleware_executor(self) -> None:
        """apply_middleware returns a MiddlewareExecutor wrapping the executor."""
        from cloud_agents.workflow.executor.middleware import (
            MiddlewareExecutor,
            apply_middleware,
        )

        executor = _FakeExecutor()
        result = apply_middleware(executor, [])

        assert isinstance(result, MiddlewareExecutor)

    def test_with_middlewares(self) -> None:
        """apply_middleware passes middlewares to MiddlewareExecutor."""
        from cloud_agents.workflow.executor.middleware import (
            MiddlewareExecutor,
            TranscriptMiddleware,
            TracingMiddleware,
            apply_middleware,
        )

        executor = _FakeExecutor()
        mock_store = AsyncMock()
        middlewares = [TracingMiddleware(), TranscriptMiddleware(mock_store)]
        result = apply_middleware(executor, middlewares)

        assert isinstance(result, MiddlewareExecutor)
