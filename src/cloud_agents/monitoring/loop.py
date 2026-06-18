"""Monitoring loop — periodic health checks with dispatch to diagnostic agent.

Runs as a background asyncio task inside the monitoring agent's FastAPI app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from agents.diagnostic.cluster_state import cluster_state
from agents.exceptions import AgentError, AgentTimeoutError, AgentUnavailableError
from agents.models import AgentRunRequest, MonitoringResult
from agents.remote_agent_client import RemoteAgentClient

logger = logging.getLogger(__name__)


class MonitoringLoop:
    """Periodic monitoring loop that checks cluster health and dispatches diagnostic.

    Attributes:
        agent_runner: The monitoring agent runner callable.
        dispatch_client: HTTP client for dispatching to the diagnostic agent.
        interval: Seconds between monitoring cycles.
    """

    def __init__(
        self,
        agent_runner: Any,
        dispatch_client: RemoteAgentClient,
        interval: int = 300,
        heartbeat_ref: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize the monitoring loop.

        Args:
            agent_runner: Async callable that runs the monitoring agent.
            dispatch_client: Client for calling the diagnostic agent.
            interval: Seconds between cycles.
            heartbeat_ref: Optional dict with 'app' key for updating last_heartbeat.
        """
        self._runner = agent_runner
        self._dispatch = dispatch_client
        self._interval = interval
        self._heartbeat_ref = heartbeat_ref
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    async def start(self) -> None:
        """Start the monitoring loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Monitoring loop started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        """Stop the monitoring loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Monitoring loop stopped")

    async def _loop(self) -> None:
        """Main loop — check and dispatch at intervals."""
        while self._running:
            self._update_heartbeat()
            try:
                await self._check_and_dispatch()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Monitoring cycle failed: %s", exc)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _check_and_dispatch(self) -> None:
        """Run one monitoring cycle. Dispatch diagnostic on critical alerts."""
        request = AgentRunRequest(prompt="Check all hosts for issues.")
        response = await self._runner(request)

        if not response.success:
            logger.warning("Monitoring run failed: %s", response.error)
            return

        result = MonitoringResult.model_validate(response.output)
        critical_alerts = [
            a for a in result.alerts if a.severity in ("high", "critical")
        ]

        if not critical_alerts:
            logger.info("Monitoring check: cluster healthy, no dispatch needed")
            return

        alert_context = "; ".join(
            f"{a.host}: {a.metric}={a.value} ({a.context})" for a in critical_alerts
        )
        logger.warning(
            "Monitoring detected %d critical alert(s), dispatching diagnostic",
            len(critical_alerts),
        )

        try:
            diag_response = await self._dispatch.run(
                prompt=f"The monitoring agent detected: {alert_context}. Investigate and fix.",
                context={"correlation_id": f"monitor-dispatch-{int(time.time())}"},
            )
            self._mark_hosts_healthy(critical_alerts)
            logger.info("Diagnostic dispatch successful")
        except (AgentUnavailableError, AgentTimeoutError, AgentError) as exc:
            logger.error("Diagnostic dispatch failed: %s", exc)

    def _mark_hosts_healthy(self, alerts: list[Any]) -> None:
        """Update local state after successful diagnostic dispatch."""
        for alert in alerts:
            host = cluster_state["hosts"].get(alert.host)
            if host:
                host["status"] = "healthy"

    def _update_heartbeat(self) -> None:
        """Update the app's heartbeat timestamp for /livez."""
        if self._heartbeat_ref and "app" in self._heartbeat_ref:
            self._heartbeat_ref["app"].state.last_heartbeat = time.monotonic()
