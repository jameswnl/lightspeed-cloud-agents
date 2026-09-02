"""Unit tests for OpenTelemetry tracing initialization and instrumentation settings.

Covers CodeRabbit findings on PR #264 (issue #263):
- init_tracing() must honor OTEL_EXPORTER_OTLP_PROTOCOL instead of always
  assuming gRPC, and must fail safe (NoOp) for an unsupported protocol.
- Agent/model instrumentation must be created with redacted settings so
  prompts, completions, tool data, and binary content aren't sent to the
  configured OTLP destination by default (CWE-532).
"""

from __future__ import annotations

import os

import pytest
from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def _reset_tracing_state(mocker: MockerFixture) -> None:
    """Reset the module-level _initialized flag so each test re-runs init_tracing()."""
    import cloud_agents.runtime.tracing as tracing_mod

    mocker.patch.object(tracing_mod, "_initialized", False)


class TestInitTracingProtocolSelection:
    """init_tracing() selects the OTLP exporter matching OTEL_EXPORTER_OTLP_PROTOCOL."""

    def test_defaults_to_grpc_exporter(self, mocker: MockerFixture) -> None:
        """With no protocol set, the gRPC exporter is used (existing/documented default)."""
        mocker.patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317"},
            clear=False,
        )
        mocker.patch.dict(os.environ, {}, clear=False)
        os.environ.pop("OTEL_EXPORTER_OTLP_PROTOCOL", None)

        # Mock the SDK plumbing too, not just the exporter classes -- the real
        # trace.set_tracer_provider() only ever takes effect once per process
        # (OTEL silently ignores later calls), so calling it for real here
        # would leak a live TracerProvider into every other test in the suite.
        # BatchSpanProcessor is mocked too so it doesn't spawn a real
        # background export thread against the mocked exporter.
        mocker.patch("cloud_agents.runtime.tracing.trace.set_tracer_provider")
        mocker.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor")
        mock_grpc_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
        )
        mock_http_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        )

        from cloud_agents.runtime.tracing import init_tracing

        init_tracing("test-service")

        mock_grpc_exporter.assert_called_once_with(endpoint="http://collector:4317")
        mock_http_exporter.assert_not_called()

    def test_http_protobuf_protocol_selects_http_exporter(self, mocker: MockerFixture) -> None:
        """OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf uses the HTTP exporter, not gRPC."""
        mocker.patch.dict(
            os.environ,
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            },
            clear=False,
        )

        # See test_defaults_to_grpc_exporter -- must not touch the real global
        # TracerProvider singleton or spawn a real export thread.
        mocker.patch("cloud_agents.runtime.tracing.trace.set_tracer_provider")
        mocker.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor")
        mock_grpc_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
        )
        mock_http_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        )

        from cloud_agents.runtime.tracing import init_tracing

        init_tracing("test-service")

        # A bare base URL must get /v1/traces appended -- the HTTP exporter
        # uses an explicit `endpoint=` kwarg as-is with no auto-append
        # (unlike its env-var-only resolution path), so passing the base
        # URL straight through would silently POST to the wrong path.
        mock_http_exporter.assert_called_once_with(endpoint="http://collector:4318/v1/traces")
        mock_grpc_exporter.assert_not_called()

    def test_http_protobuf_protocol_does_not_double_append_trace_path(
        self, mocker: MockerFixture
    ) -> None:
        """An endpoint that already ends in /v1/traces is left unchanged."""
        mocker.patch.dict(
            os.environ,
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/v1/traces",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            },
            clear=False,
        )

        mocker.patch("cloud_agents.runtime.tracing.trace.set_tracer_provider")
        mocker.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor")
        mock_http_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        )

        from cloud_agents.runtime.tracing import init_tracing

        init_tracing("test-service")

        mock_http_exporter.assert_called_once_with(endpoint="http://collector:4318/v1/traces")

    def test_unsupported_protocol_disables_tracing(self, mocker: MockerFixture) -> None:
        """An unrecognized protocol fails safe to NoOp instead of using the wrong transport."""
        mocker.patch.dict(
            os.environ,
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "carrier-pigeon",
            },
            clear=False,
        )

        mock_set_provider = mocker.patch("cloud_agents.runtime.tracing.trace.set_tracer_provider")
        mock_grpc_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
        )
        mock_http_exporter = mocker.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        )

        from cloud_agents.runtime.tracing import init_tracing

        init_tracing("test-service")

        mock_grpc_exporter.assert_not_called()
        mock_http_exporter.assert_not_called()
        mock_set_provider.assert_not_called()


class TestGetInstrumentationSettings:
    """Shared pydantic-ai InstrumentationSettings used across spawn: none/local (issue #263)."""

    def test_redacts_content_and_binary_content(self) -> None:
        """Prompts/completions/tool data and binary content are excluded from spans."""
        from cloud_agents.runtime.tracing import get_instrumentation_settings

        settings = get_instrumentation_settings()

        assert settings.include_content is False
        assert settings.include_binary_content is False
