"""Unit tests for generic AgentLoop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.exceptions import AgentUnavailableError
from agents.models import AgentRunResponse
from agents.runtime.agent_loop import AgentLoop


def _make_response(
    cluster_healthy: bool = True,
    alerts: list | None = None,
) -> AgentRunResponse:
    """Create a mock agent response."""
    return AgentRunResponse(
        output={
            "alerts": alerts or [],
            "cluster_healthy": cluster_healthy,
        },
        output_type="MonitoringResult",
        usage={"input_tokens": 10, "output_tokens": 20},
        agent_name="test-agent",
        success=True,
    )


class TestCheckAndDispatch:
    """Tests for _check_and_dispatch."""

    @pytest.mark.asyncio
    async def test_no_alerts_no_dispatch(self) -> None:
        """Test that no alerts means no dispatch."""
        runner = AsyncMock(return_value=_make_response())
        dispatch = AsyncMock()
        loop = AgentLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        ids = await loop._check_and_dispatch()
        assert ids == []
        dispatch.run_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_critical_alert_dispatches(self) -> None:
        """Test that critical alerts trigger dispatch."""
        alerts = [
            {"host": "web-02", "metric": "cpu", "value": "92%", "severity": "critical"}
        ]
        runner = AsyncMock(return_value=_make_response(False, alerts))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-123")
        loop = AgentLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        ids = await loop._check_and_dispatch()
        assert ids == ["run-123"]
        dispatch.run_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_failure_survives(self) -> None:
        """Test that dispatch failure doesn't crash the loop."""
        alerts = [{"host": "h", "metric": "m", "value": "v", "severity": "high"}]
        runner = AsyncMock(return_value=_make_response(False, alerts))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(side_effect=AgentUnavailableError("down"))
        loop = AgentLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        ids = await loop._check_and_dispatch()
        assert ids == []

    @pytest.mark.asyncio
    async def test_on_dispatch_success_callback(self) -> None:
        """Test that the post-dispatch callback is invoked."""
        alerts = [
            {"host": "web-02", "metric": "cpu", "value": "92%", "severity": "critical"}
        ]
        runner = AsyncMock(return_value=_make_response(False, alerts))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-456")
        callback = MagicMock()
        loop = AgentLoop(
            agent_runner=runner,
            dispatch_client=dispatch,
            interval=0,
            on_dispatch_success=callback,
        )
        await loop._check_and_dispatch()
        callback.assert_called_once_with(alerts)

    @pytest.mark.asyncio
    async def test_no_callback_when_not_configured(self) -> None:
        """Test that dispatch works without a callback."""
        alerts = [{"host": "h", "metric": "m", "value": "v", "severity": "high"}]
        runner = AsyncMock(return_value=_make_response(False, alerts))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-789")
        loop = AgentLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        ids = await loop._check_and_dispatch()
        assert ids == ["run-789"]


class TestLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        """Test clean start and stop."""
        runner = AsyncMock(return_value=_make_response())
        dispatch = AsyncMock()
        loop = AgentLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        await loop.start()
        await asyncio.sleep(0.05)
        await loop.stop()
        assert runner.call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """Test that stopping without starting doesn't crash."""
        loop = AgentLoop(
            agent_runner=AsyncMock(), dispatch_client=AsyncMock(), interval=0
        )
        await loop.stop()
