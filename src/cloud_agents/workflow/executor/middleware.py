"""StepMiddleware protocol for cross-cutting executor concerns.

Middleware wraps executor.run() calls with pre/post hooks. Applied
uniformly across all spawn modes (none, local, ephemeral) and all
runners (WorkflowRunner, ChatWorkflowRunner).

The MiddlewareExecutor owns the OTEL span lifecycle around step
execution. Middleware before()/after() hooks run inside that span.

No temporalio imports.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional, Protocol

from opentelemetry.trace import StatusCode, Tracer

from cloud_agents.workflow.executor.step.base import (
    StepInput,
    StepMetadata,
    StepResult,
    StreamEvent,
)

logger = logging.getLogger(__name__)


class StepMiddleware(Protocol):
    """Pre/post hooks around step execution.

    Middleware implementations are called by MiddlewareExecutor:
    - before(): called before executor.run(), may modify step_input.
    - after(): called after executor.run(), may modify result.
    """

    async def before(self, step_input: StepInput) -> StepInput:
        """Called before executor.run(). May modify step_input.

        Parameters:
            step_input: Step execution input.

        Returns:
            Possibly modified step_input.
        """
        ...

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Called after executor.run(). May modify result.

        Parameters:
            step_input: Step execution input (after before() modifications).
            result: Step execution result.

        Returns:
            Possibly modified result.
        """
        ...


class TracingMiddleware:
    """OTEL trace_id propagation and span attribute enrichment.

    before(): extracts trace_id from the current OTEL span context
    and sets it on step_input.metadata for downstream correlation.

    after(): sets step.status, input_tokens, output_tokens as
    attributes on the current OTEL span.
    """

    async def before(self, step_input: StepInput) -> StepInput:
        """Extract trace_id from current span context and set on metadata.

        Parameters:
            step_input: Step execution input.

        Returns:
            Step input with trace_id set on metadata (if tracing active).
        """
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            if step_input.metadata is None:
                step_input.metadata = StepMetadata()
            step_input.metadata.trace_id = format(ctx.trace_id, "032x")
        return step_input

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Set result attributes on current OTEL span.

        Parameters:
            step_input: Step execution input.
            result: Step execution result.

        Returns:
            Unmodified result.
        """
        from opentelemetry import trace

        span = trace.get_current_span()
        span.set_attribute("step.status", result.status)
        span.set_attribute("step.input_tokens", result.input_tokens)
        span.set_attribute("step.output_tokens", result.output_tokens)
        return result


class TranscriptMiddleware:
    """Saves conversation messages and transcript to TranscriptStore.

    before(): no-op (pass-through).
    after(): persists user/assistant messages and StepTranscript to the
    configured store. Resilient -- catches and logs exceptions.
    """

    def __init__(self, transcript_store: Any) -> None:
        """Initialize with a TranscriptStore instance.

        Parameters:
            transcript_store: TranscriptStore for persistence (may be None).
        """
        self._store = transcript_store

    async def before(self, step_input: StepInput) -> StepInput:
        """No-op pass-through.

        Parameters:
            step_input: Step execution input.

        Returns:
            Unmodified step_input.
        """
        return step_input

    async def after(self, step_input: StepInput, result: StepResult) -> StepResult:
        """Save conversation messages to transcript store.

        Parameters:
            step_input: Step execution input.
            result: Step execution result.

        Returns:
            Unmodified result.
        """
        if not self._store:
            return result

        try:
            from cloud_agents.workflow.core.models import StepTranscript
            from cloud_agents.workflow.executor.step.conversation import (
                ConversationMessage,
            )

            messages: list[dict[str, Any]] = [
                ConversationMessage(role="user", content=step_input.prompt).to_dict(),
            ]
            if result.output is not None:
                content = (
                    json.dumps(result.output)
                    if isinstance(result.output, dict)
                    else str(result.output)
                )
                messages.append(
                    ConversationMessage(role="assistant", content=content).to_dict()
                )

            step_name = step_input.output_key or step_input.step_name

            await self._store.save(
                workflow_id=step_input.workflow_id,
                step_name=step_name,
                transcript=StepTranscript(
                    step_name=step_name,
                    events=result.transcript,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    duration_ms=result.duration_ms,
                ),
                trace_id=(
                    step_input.metadata.trace_id if step_input.metadata else None
                ),
                messages=messages,
            )
        except Exception:
            logger.warning(
                "Failed to save transcript for step '%s'",
                step_input.step_name,
                exc_info=True,
            )

        return result


class MiddlewareExecutor:
    """Wraps a StepExecutor with a middleware stack and OTEL span.

    Owns the OTEL span lifecycle around step execution:
    - run(): opens span -> before hooks -> executor.run() -> after hooks -> close span
    - run_stream(): opens span -> before hooks -> executor.run_stream() -> after hooks

    Before hooks run in list order. After hooks run in reverse order.

    Attributes:
        _executor: Wrapped StepExecutor.
        _middlewares: Ordered list of middleware to apply.
        _tracer: Optional OTEL tracer for span creation.
    """

    def __init__(
        self,
        executor: Any,
        middlewares: list[StepMiddleware],
        tracer: Optional[Tracer] = None,
    ) -> None:
        """Initialize the middleware executor.

        Parameters:
            executor: StepExecutor to wrap.
            middlewares: Ordered list of StepMiddleware implementations.
            tracer: Optional OTEL tracer. If None, uses default tracer.
        """
        self._executor = executor
        self._middlewares = middlewares
        self._tracer = tracer

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute with middleware stack and OTEL span.

        Opens an OTEL span, runs before hooks, executes the step,
        runs after hooks (reverse), then closes the span.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult from the executor, possibly modified by after hooks.

        Raises:
            Exception: Re-raises any exception from the executor.
        """
        if self._tracer:
            return await self._run_with_span(step_input)
        return await self._run_no_span(step_input)

    async def _run_no_span(self, step_input: StepInput) -> StepResult:
        """Execute with middleware but no OTEL span.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult from executor.
        """
        for mw in self._middlewares:
            step_input = await mw.before(step_input)

        result = await self._executor.run(step_input)

        for mw in reversed(self._middlewares):
            result = await mw.after(step_input, result)

        return result

    async def _run_with_span(self, step_input: StepInput) -> StepResult:
        """Execute with middleware inside an OTEL span.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult from executor.
        """
        assert self._tracer is not None

        with self._tracer.start_as_current_span(
            "step.execute",
            attributes={
                "step.name": step_input.step_name,
                "workflow.id": step_input.workflow_id,
            },
        ) as span:
            for mw in self._middlewares:
                step_input = await mw.before(step_input)

            try:
                result = await self._executor.run(step_input)
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                raise

            for mw in reversed(self._middlewares):
                result = await mw.after(step_input, result)

            return result

    async def run_stream(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Stream with middleware stack inside an OTEL span.

        Applies before hooks, streams from executor within a tracing span,
        then applies after hooks on the final result (complete or error event).

        Parameters:
            step_input: Step execution input.

        Yields:
            StreamEvent instances from the executor.
        """
        if self._tracer:
            async for event in self._run_stream_with_span(step_input):
                yield event
        else:
            async for event in self._run_stream_no_span(step_input):
                yield event

    async def _run_stream_no_span(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Stream without OTEL span."""
        for mw in self._middlewares:
            step_input = await mw.before(step_input)

        final_result: Optional[StepResult] = None
        async for event in self._executor.run_stream(step_input):
            yield event
            if event.type in ("complete", "error") and event.result:
                final_result = event.result

        if final_result:
            for mw in reversed(self._middlewares):
                await mw.after(step_input, final_result)

    async def _run_stream_with_span(self, step_input: StepInput) -> AsyncIterator[StreamEvent]:
        """Stream inside an OTEL span."""
        assert self._tracer is not None

        with self._tracer.start_as_current_span(
            "step.execute.stream",
            attributes={
                "step.name": step_input.step_name,
                "workflow.id": step_input.workflow_id,
            },
        ) as span:
            for mw in self._middlewares:
                step_input = await mw.before(step_input)

            final_result: Optional[StepResult] = None
            async for event in self._executor.run_stream(step_input):
                yield event
                if event.type in ("complete", "error") and event.result:
                    final_result = event.result

            if final_result:
                for mw in reversed(self._middlewares):
                    await mw.after(step_input, final_result)


def apply_middleware(
    executor: Any,
    middlewares: list[StepMiddleware],
    tracer: Optional[Tracer] = None,
) -> MiddlewareExecutor:
    """Wrap an executor with a middleware stack.

    Parameters:
        executor: StepExecutor to wrap.
        middlewares: Ordered list of middleware to apply.
        tracer: Optional OTEL tracer for span creation.

    Returns:
        MiddlewareExecutor wrapping the executor.
    """
    return MiddlewareExecutor(executor, middlewares, tracer=tracer)
