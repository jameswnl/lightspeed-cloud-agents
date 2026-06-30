"""Generic agent loop — periodic health checks with dispatch.

Generalized from Phase 1b's MonitoringLoop. Supports configurable
post-dispatch callbacks via on_dispatch_success hook.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Optional

from cloud_agents.exceptions import AgentError, AgentTimeoutError, AgentUnavailableError
from cloud_agents.models import AgentRunRequest
from cloud_agents.remote_agent_client import RemoteAgentClient

logger = logging.getLogger(__name__)


class AgentLoop:
    """Periodic agent loop that checks for issues and dispatches another agent.

    Attributes:
        agent_runner: The agent runner callable.
        dispatch_client: HTTP client for dispatching to another agent.
        interval: Seconds between cycles.
        on_dispatch_success: Optional callback after successful dispatch.
    """

    def __init__(
        self,
        agent_runner: Any,
        dispatch_client: RemoteAgentClient,
        interval: int = 300,
        heartbeat_ref: Optional[dict[str, Any]] = None,
        on_dispatch_success: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Initialize the agent loop.

        Args:
            agent_runner: Async callable that runs the agent.
            dispatch_client: Client for calling the dispatch target.
            interval: Seconds between cycles.
            heartbeat_ref: Optional dict with 'app' key for /livez heartbeat.
            on_dispatch_success: Optional callback invoked after successful dispatch.
        """
        self._runner = agent_runner
        self._dispatch = dispatch_client
        self._interval = interval
        self._heartbeat_ref = heartbeat_ref
        self._on_success = on_dispatch_success
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    async def start(self) -> None:
        """Start the loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Agent loop started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        """Stop the loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Agent loop stopped")

    async def _loop(self) -> None:
        """Main loop — check and dispatch at intervals."""
        while self._running:
            self._update_heartbeat()
            try:
                await self._check_and_dispatch()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Loop cycle failed: %s", exc)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _check_and_dispatch(self) -> list[str]:
        """Run one cycle. Dispatch on critical alerts.

        Returns:
            List of dispatched run IDs.
        """
        request = AgentRunRequest(prompt="Check all hosts for issues.")
        response = await self._runner(request)

        if not response.success:
            logger.warning("Agent check failed: %s", response.error)
            return []

        output = response.output
        alerts = output.get("alerts", [])
        critical = [a for a in alerts if a.get("severity") in ("high", "critical")]

        if not critical:
            logger.info("Check complete: no critical alerts")
            return []

        context_str = "; ".join(
            f"{a.get('host', '?')}: {a.get('metric', '?')}={a.get('value', '?')}"
            for a in critical
        )
        logger.warning("Detected %d critical alert(s), dispatching", len(critical))

        dispatched: list[str] = []
        try:
            run_id = await self._dispatch.run_async(
                prompt=f"Alerts detected: {context_str}. Investigate and fix.",
                context={"correlation_id": f"loop-dispatch-{int(time.time())}"},
            )
            dispatched.append(run_id)
            if self._on_success:
                self._on_success(critical)
            logger.info("Dispatch successful, run_id=%s", run_id)
        except (AgentUnavailableError, AgentTimeoutError, AgentError) as exc:
            logger.error("Dispatch failed: %s", exc)

        return dispatched

    def _update_heartbeat(self) -> None:
        """Update the app's heartbeat for /livez."""
        if self._heartbeat_ref and "app" in self._heartbeat_ref:
            self._heartbeat_ref["app"].state.last_heartbeat = time.monotonic()
