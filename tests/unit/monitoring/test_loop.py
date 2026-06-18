"""Unit tests for MonitoringLoop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.diagnostic.cluster_state import init_scenario
from agents.exceptions import AgentUnavailableError
from agents.models import AgentRunResponse, MonitoringResult
from agents.monitoring.loop import MonitoringLoop


def _make_monitoring_response(
    cluster_healthy: bool = True,
    alerts: list | None = None,
) -> AgentRunResponse:
    """Create a mock monitoring agent response."""
    result = MonitoringResult(
        alerts=alerts or [],
        cluster_healthy=cluster_healthy,
    )
    return AgentRunResponse(
        output=result.model_dump(),
        output_type="MonitoringResult",
        usage={"input_tokens": 10, "output_tokens": 20},
        agent_name="monitoring-agent",
        success=True,
    )


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cluster state."""
    init_scenario("healthy")


class TestCheckAndDispatch:
    """Tests for _check_and_dispatch as a standalone coroutine."""

    @pytest.mark.asyncio
    async def test_healthy_cluster_no_dispatch(self) -> None:
        """Test that a healthy cluster does not dispatch."""
        runner = AsyncMock(return_value=_make_monitoring_response())
        dispatch = AsyncMock()
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)

        await loop._check_and_dispatch()

        runner.assert_called_once()
        dispatch.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_critical_alert_dispatches(self) -> None:
        """Test that critical alerts trigger diagnostic dispatch."""
        alerts = [{
            "host": "web-02",
            "metric": "cpu",
            "value": "92%",
            "severity": "critical",
            "context": "CPU spike",
            "recommended_action": "investigate",
        }]
        runner = AsyncMock(return_value=_make_monitoring_response(
            cluster_healthy=False, alerts=alerts
        ))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-test-123")
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)

        dispatched_ids = await loop._check_and_dispatch()

        dispatch.run_async.assert_called_once()
        call_kwargs = dispatch.run_async.call_args
        assert "web-02" in call_kwargs.kwargs.get("prompt", call_kwargs[1].get("prompt", ""))
        assert dispatched_ids == ["run-test-123"]

    @pytest.mark.asyncio
    async def test_dispatch_failure_does_not_crash(self) -> None:
        """Test that a dispatch failure is caught and logged."""
        alerts = [{
            "host": "web-02", "metric": "cpu", "value": "92%",
            "severity": "high", "context": "spike",
            "recommended_action": "check",
        }]
        runner = AsyncMock(return_value=_make_monitoring_response(
            cluster_healthy=False, alerts=alerts
        ))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(side_effect=AgentUnavailableError("connection refused"))
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)

        await loop._check_and_dispatch()

    @pytest.mark.asyncio
    async def test_successful_dispatch_updates_local_state(self) -> None:
        """Test that local state is updated after successful dispatch."""
        init_scenario("bad_deploy")
        from agents.diagnostic.cluster_state import cluster_state
        assert cluster_state["hosts"]["web-02"]["status"] == "degraded"

        alerts = [{
            "host": "web-02", "metric": "status", "value": "degraded",
            "severity": "critical", "context": "degraded",
            "recommended_action": "fix",
        }]
        runner = AsyncMock(return_value=_make_monitoring_response(
            cluster_healthy=False, alerts=alerts
        ))
        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-test-123")
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)

        await loop._check_and_dispatch()

        assert cluster_state["hosts"]["web-02"]["status"] == "healthy"


class TestLoopLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        """Test that the loop starts and stops cleanly."""
        runner = AsyncMock(return_value=_make_monitoring_response())
        dispatch = AsyncMock()
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)

        await loop.start()
        await asyncio.sleep(0.05)
        await loop.stop()

        assert runner.call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """Test that stopping a non-started loop does not crash."""
        runner = AsyncMock()
        dispatch = AsyncMock()
        loop = MonitoringLoop(agent_runner=runner, dispatch_client=dispatch, interval=0)
        await loop.stop()


class TestHeartbeat:
    """Tests for heartbeat updates."""

    @pytest.mark.asyncio
    async def test_heartbeat_updated_on_cycle(self) -> None:
        """Test that heartbeat is updated during each monitoring cycle."""
        import time

        class FakeApp:
            class state:
                last_heartbeat = 0.0

        runner = AsyncMock(return_value=_make_monitoring_response())
        dispatch = AsyncMock()
        loop = MonitoringLoop(
            agent_runner=runner,
            dispatch_client=dispatch,
            interval=0,
            heartbeat_ref={"app": FakeApp()},
        )

        await loop._check_and_dispatch()
        loop._update_heartbeat()

        assert FakeApp.state.last_heartbeat > 0


class TestRedispatchPrevention:
    """Tests for preventing repeated redispatch of the same issue."""

    @pytest.mark.asyncio
    async def test_second_cycle_skips_fixed_host(self) -> None:
        """Test that after dispatch+state mutation, a second cycle does not redispatch."""
        init_scenario("bad_deploy")

        alerts = [{
            "host": "web-02", "metric": "status", "value": "degraded",
            "severity": "critical", "context": "degraded",
            "recommended_action": "fix",
        }]

        call_count = 0

        async def mock_runner(req):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_monitoring_response(cluster_healthy=False, alerts=alerts)
            return _make_monitoring_response(cluster_healthy=True, alerts=[])

        dispatch = AsyncMock()
        dispatch.run_async = AsyncMock(return_value="run-test-123")
        loop = MonitoringLoop(agent_runner=mock_runner, dispatch_client=dispatch, interval=0)

        await loop._check_and_dispatch()
        assert dispatch.run_async.call_count == 1

        await loop._check_and_dispatch()
        assert dispatch.run_async.call_count == 1
