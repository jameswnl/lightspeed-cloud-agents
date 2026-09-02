"""OpenTelemetry distributed tracing for agent runtime.

Provides centralized tracer initialization and context propagation.
When OTEL_EXPORTER_OTLP_ENDPOINT is unset, uses NoOpTracerProvider
for zero overhead in dev/test.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, StatusCode, Tracer

if TYPE_CHECKING:
    from pydantic_ai.models.instrumented import InstrumentationSettings

logger = logging.getLogger(__name__)

_initialized = False

_SUPPORTED_OTLP_PROTOCOLS = ("grpc", "http/protobuf")


def init_tracing(service_name: str) -> None:
    """Initialize OpenTelemetry tracing.

    When OTEL_EXPORTER_OTLP_ENDPOINT is set, configures OTLP export using the
    exporter matching OTEL_EXPORTER_OTLP_PROTOCOL ("grpc", the default, or
    "http/protobuf"). An unsupported protocol value fails safe to NoOp rather
    than silently using a mismatched transport. Otherwise uses NoOp provider
    (zero overhead).

    Args:
        service_name: Service name for trace attributes.
    """
    global _initialized
    if _initialized:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled (NoOp)")
        _initialized = True
        return

    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    elif protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        logger.warning(
            "Unsupported OTEL_EXPORTER_OTLP_PROTOCOL=%r (expected one of %s) "
            "— tracing disabled (NoOp)",
            protocol,
            _SUPPORTED_OTLP_PROTOCOLS,
        )
        _initialized = True
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    logger.info(
        "OpenTelemetry tracing initialized: service=%s, endpoint=%s, protocol=%s",
        service_name,
        endpoint,
        protocol,
    )
    _initialized = True


def get_instrumentation_settings() -> "InstrumentationSettings":
    """Redacted pydantic-ai instrumentation settings for agent/model spans.

    Excludes prompt/completion/tool content and binary content from span
    attributes -- the configured OTLP destination isn't vetted to receive
    that content (CWE-532, issue #263).

    Returns:
        InstrumentationSettings with include_content and
        include_binary_content disabled.
    """
    from pydantic_ai.models.instrumented import InstrumentationSettings

    return InstrumentationSettings(include_content=False, include_binary_content=False)


def get_tracer(name: str) -> Tracer:
    """Get a tracer instance.

    Args:
        name: Module or component name for the tracer.

    Returns:
        OpenTelemetry Tracer instance.
    """
    return trace.get_tracer(name)


def extract_traceparent(headers: dict[str, str]) -> Optional[Context]:
    """Extract trace context from incoming HTTP headers.

    Args:
        headers: HTTP request headers.

    Returns:
        Extracted context, or None if no trace context found.
    """
    from opentelemetry.propagate import extract

    ctx = extract(headers)
    return ctx


def inject_traceparent(headers: dict[str, str]) -> dict[str, str]:
    """Inject trace context into outgoing HTTP headers.

    Args:
        headers: HTTP headers dict to inject into.

    Returns:
        Updated headers dict with traceparent/tracestate.
    """
    from opentelemetry.propagate import inject

    inject(headers)
    return headers


def set_span_error(span: Span, exc: Exception) -> None:
    """Record an error on the current span.

    Args:
        span: The active span.
        exc: The exception to record.
    """
    span.set_status(StatusCode.ERROR, str(exc))
    span.record_exception(exc)
