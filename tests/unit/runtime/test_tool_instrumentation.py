"""Unit tests for per-tool Prometheus metrics instrumentation."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from agents.runtime.tool_instrumentation import instrument_tool


def _get_metric(name: str, labels: dict) -> float | None:
    """Get a metric sample value from the Prometheus registry."""
    return REGISTRY.get_sample_value(name + "_total", labels)


def _get_histogram_count(name: str, labels: dict) -> float | None:
    """Get histogram sample count from the Prometheus registry."""
    return REGISTRY.get_sample_value(name + "_count", labels)


class TestInstrumentTool:
    """Tests for the instrument_tool wrapper."""

    def test_successful_call_records_counter(self) -> None:
        """Test that a successful tool call increments the success counter."""
        def my_tool(host: str) -> str:
            return f"checked {host}"

        wrapped = instrument_tool(my_tool, "test-agent-1", "check_host")
        result = wrapped("web-02")

        assert result == "checked web-02"
        count = _get_metric(
            "ls_agent_tool_calls",
            {"agent_name": "test-agent-1", "tool_name": "check_host", "status": "success"},
        )
        assert count is not None and count >= 1.0

    def test_successful_call_records_duration(self) -> None:
        """Test that a successful tool call observes duration histogram."""
        def my_tool() -> str:
            return "done"

        wrapped = instrument_tool(my_tool, "test-agent-2", "fast_tool")
        wrapped()

        hist_count = _get_histogram_count(
            "ls_agent_tool_duration_seconds",
            {"agent_name": "test-agent-2", "tool_name": "fast_tool"},
        )
        assert hist_count is not None and hist_count >= 1.0

    def test_failed_call_records_error_counter(self) -> None:
        """Test that a failed tool call records error status counter."""
        def failing_tool() -> str:
            raise RuntimeError("disk full")

        wrapped = instrument_tool(failing_tool, "test-agent-3", "check_disk")

        with pytest.raises(RuntimeError, match="disk full"):
            wrapped()

        count = _get_metric(
            "ls_agent_tool_calls",
            {"agent_name": "test-agent-3", "tool_name": "check_disk", "status": "error"},
        )
        assert count is not None and count >= 1.0

    def test_preserves_function_name(self) -> None:
        """Test that the wrapped function preserves the original name."""
        def list_hosts() -> list[str]:
            """List all hosts."""
            return ["web-01", "web-02"]

        wrapped = instrument_tool(list_hosts, "test-agent-4", "list_hosts")
        assert wrapped.__name__ == "list_hosts"
        assert wrapped.__doc__ == "List all hosts."

    def test_passes_args_and_kwargs(self) -> None:
        """Test that args and kwargs are forwarded correctly."""
        def run_remediation(host: str, action: str, reason: str = "auto") -> dict:
            return {"host": host, "action": action, "reason": reason}

        wrapped = instrument_tool(run_remediation, "test-agent-5", "run_remediation")
        result = wrapped("web-02", "restart", reason="cpu spike")

        assert result == {"host": "web-02", "action": "restart", "reason": "cpu spike"}


class TestInstrumentToolAsync:
    """Tests for async tool instrumentation."""

    @pytest.mark.asyncio
    async def test_async_tool_success_records_counter(self) -> None:
        """Test that async tools record success counter."""
        async def async_check(host: str) -> str:
            return f"async checked {host}"

        wrapped = instrument_tool(async_check, "test-agent-6", "async_check")
        result = await wrapped("web-01")

        assert result == "async checked web-01"
        count = _get_metric(
            "ls_agent_tool_calls",
            {"agent_name": "test-agent-6", "tool_name": "async_check", "status": "success"},
        )
        assert count is not None and count >= 1.0

    @pytest.mark.asyncio
    async def test_async_tool_failure_records_error(self) -> None:
        """Test that async tool failures record error counter."""
        async def async_fail() -> str:
            raise ValueError("bad input")

        wrapped = instrument_tool(async_fail, "test-agent-7", "async_fail")

        with pytest.raises(ValueError, match="bad input"):
            await wrapped()

        count = _get_metric(
            "ls_agent_tool_calls",
            {"agent_name": "test-agent-7", "tool_name": "async_fail", "status": "error"},
        )
        assert count is not None and count >= 1.0
