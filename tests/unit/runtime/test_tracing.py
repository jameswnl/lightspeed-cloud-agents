"""Unit tests for OpenTelemetry tracing module."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

import agents.runtime.tracing as tracing_module
from agents.runtime.tracing import (
    extract_traceparent,
    get_tracer,
    inject_traceparent,
    init_tracing,
    set_span_error,
)


class ListSpanExporter(SpanExporter):
    """Simple exporter that collects spans in a list for testing."""

    def __init__(self) -> None:
        """Initialize with empty span list."""
        self.spans: list[ReadableSpan] = []

    def export(self, spans):
        """Collect spans."""
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """No-op shutdown."""

    def force_flush(self, timeout_millis: int = 0) -> bool:
        """No-op flush."""
        return True


def _setup_test_provider() -> tuple[TracerProvider, ListSpanExporter]:
    """Create a test TracerProvider with a ListSpanExporter."""
    exporter = ListSpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


@pytest.fixture(autouse=True)
def _reset_tracing():
    """Reset tracing state between tests."""
    tracing_module._initialized = False
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    yield
    tracing_module._initialized = False


class TestInitTracing:
    """Tests for init_tracing."""

    def test_noop_when_no_endpoint(self) -> None:
        """Test that tracing is noop when OTEL_EXPORTER_OTLP_ENDPOINT is unset."""
        with patch.dict("os.environ", {}, clear=False):
            if "OTEL_EXPORTER_OTLP_ENDPOINT" in __import__("os").environ:
                pytest.skip("OTEL_EXPORTER_OTLP_ENDPOINT is set in env")
            init_tracing("test-service")
            assert tracing_module._initialized is True

    def test_idempotent(self) -> None:
        """Test that init_tracing only initializes once."""
        init_tracing("test-service")
        init_tracing("test-service")
        assert tracing_module._initialized is True


class TestGetTracer:
    """Tests for get_tracer."""

    def test_returns_tracer(self) -> None:
        """Test that get_tracer returns a valid tracer."""
        tracer = get_tracer("test-module")
        assert tracer is not None


class TestSpanCreation:
    """Tests for span creation and attributes."""

    def test_creates_span_with_attributes(self) -> None:
        """Test creating a span with custom attributes."""
        _, exporter = _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("agent.run.diagnostic") as span:
            span.set_attribute("agent.name", "diagnostic-agent")
            span.set_attribute("correlation.id", "abc-123")

        assert len(exporter.spans) == 1
        assert exporter.spans[0].name == "agent.run.diagnostic"
        assert exporter.spans[0].attributes["agent.name"] == "diagnostic-agent"
        assert exporter.spans[0].attributes["correlation.id"] == "abc-123"

    def test_nested_spans(self) -> None:
        """Test parent-child span relationships."""
        _, exporter = _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("workflow.test-wf") as parent:
            with tracer.start_as_current_span("workflow.step.diagnose") as child:
                child.set_attribute("step.name", "diagnose")

        assert len(exporter.spans) == 2
        child_span = exporter.spans[0]
        parent_span = exporter.spans[1]
        assert child_span.parent.span_id == parent_span.context.span_id

    def test_error_recording(self) -> None:
        """Test recording an error on a span."""
        _, exporter = _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("agent.run.failing") as span:
            exc = RuntimeError("agent crashed")
            set_span_error(span, exc)

        assert len(exporter.spans) == 1
        assert exporter.spans[0].status.status_code.name == "ERROR"
        assert len(exporter.spans[0].events) == 1
        assert exporter.spans[0].events[0].name == "exception"


class TestContextPropagation:
    """Tests for trace context propagation."""

    def test_inject_traceparent(self) -> None:
        """Test injecting traceparent into outgoing headers."""
        _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("outgoing"):
            headers: dict[str, str] = {}
            inject_traceparent(headers)

        assert "traceparent" in headers
        assert headers["traceparent"].startswith("00-")

    def test_extract_traceparent(self) -> None:
        """Test extracting trace context from incoming headers."""
        _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("sender"):
            headers: dict[str, str] = {}
            inject_traceparent(headers)

        ctx = extract_traceparent(headers)
        assert ctx is not None

    def test_round_trip_context(self) -> None:
        """Test that inject → extract preserves trace context."""
        _, exporter = _setup_test_provider()

        tracer = get_tracer("test")
        with tracer.start_as_current_span("parent") as parent_span:
            headers: dict[str, str] = {}
            inject_traceparent(headers)
            parent_trace_id = parent_span.context.trace_id

        ctx = extract_traceparent(headers)
        with tracer.start_as_current_span("child", context=ctx) as child_span:
            child_span.set_attribute("test", True)

        assert len(exporter.spans) == 2
        child = exporter.spans[0]
        assert child.context.trace_id == parent_trace_id
