"""Workflow advancement and recovery for stateless execution.

Handles callback-triggered advancement and background recovery
polling for orphaned steps. Uses optimistic locking to prevent
duplicate processing across replicas.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from agents.workflow.persistence import WorkflowPersistence
from agents.workflow.state import WorkflowState

logger = logging.getLogger(__name__)

RECOVERY_POLL_INTERVAL = 60


class StaleStateError(Exception):
    """Raised when optimistic lock fails due to version mismatch."""


async def save_with_version(
    persistence: WorkflowPersistence,
    state: WorkflowState,
    expected_version: int,
) -> None:
    """Save workflow state with optimistic locking.

    Increments version on save. Raises StaleStateError if
    the current version doesn't match expected.

    Args:
        persistence: The persistence backend.
        state: The workflow state to save.
        expected_version: The version we expect to be current.

    Raises:
        StaleStateError: If another replica already advanced this workflow.
    """
    state.updated_at = datetime.now(timezone.utc).isoformat()
    if hasattr(persistence, "save_cas"):
        success = await persistence.save_cas(state, expected_version)
        if not success:
            raise StaleStateError(
                f"Workflow {state.workflow_id} version mismatch: "
                f"expected {expected_version}"
            )
    else:
        current = await persistence.load(state.workflow_id)
        if current and current.version != expected_version:
            raise StaleStateError(
                f"Workflow {state.workflow_id} version mismatch: "
                f"expected {expected_version}, got {current.version}"
            )
        state.version = expected_version + 1
        await persistence.save(state)


class RecoveryPoller:
    """Background poller that detects orphaned dispatched steps.

    Runs on every runner replica. Checks for steps stuck in
    "dispatched" status past their timeout and marks them failed.

    Attributes:
        persistence: The persistence backend.
        poll_interval: Seconds between poll cycles.
        step_timeout: Seconds before a dispatched step is considered orphaned.
    """

    def __init__(
        self,
        persistence: WorkflowPersistence,
        poll_interval: int = RECOVERY_POLL_INTERVAL,
        step_timeout: int = 600,
    ) -> None:
        """Initialize the recovery poller.

        Args:
            persistence: The persistence backend.
            poll_interval: Seconds between poll cycles.
            step_timeout: Seconds before orphan detection.
        """
        self._persistence = persistence
        self._poll_interval = poll_interval
        self._step_timeout = step_timeout
        self._running = False

    async def start(self) -> None:
        """Start the recovery polling loop."""
        self._running = True
        logger.info("Recovery poller started (interval=%ds)", self._poll_interval)
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.warning("Recovery poll error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop the recovery polling loop."""
        self._running = False
        logger.info("Recovery poller stopped")

    async def _poll_once(self) -> None:
        """Check for orphaned dispatched steps."""
        workflows = await self._persistence.list_active()
        now = datetime.now(timezone.utc)
        for wf in workflows:
            if wf.status != "running":
                continue
            for key, step_result in wf.steps.items():
                if step_result.status != "dispatched":
                    continue
                if not step_result.started_at:
                    continue
                started = datetime.fromisoformat(step_result.started_at)
                elapsed = (now - started).total_seconds()
                if elapsed > self._step_timeout:
                    logger.warning(
                        "Orphaned step '%s' in workflow '%s' (dispatched %ds ago)",
                        step_result.step_name, wf.workflow_id, int(elapsed),
                    )
                    step_result.status = "failed"
                    step_result.error = f"Step timed out after {int(elapsed)}s (orphaned)"
                    step_result.completed_at = now.isoformat()
                    wf.status = "failed"
                    try:
                        await save_with_version(self._persistence, wf, wf.version)
                    except StaleStateError:
                        logger.info("Another replica already handled workflow %s", wf.workflow_id)
