"""In-memory store for async agent run state.

Stores RunState objects keyed by run_id. Runs expire after a
configurable TTL. Uses asyncio.Lock for safe concurrent access.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Optional

from cloud_agents.models import AgentRunResponse, RunState, RunStatus


class RunStore:
    """In-memory store for async agent runs.

    Attributes:
        expiry_seconds: How long to keep completed/failed runs before cleanup.
    """

    def __init__(self, expiry_seconds: int = 3600) -> None:
        """Initialize the store.

        Args:
            expiry_seconds: TTL for run entries in seconds.
        """
        self._runs: dict[str, tuple[RunState, float]] = {}
        self._lock = asyncio.Lock()
        self._expiry_seconds = expiry_seconds

    async def create_run(self) -> RunState:
        """Create a new run in RUNNING state.

        Returns:
            The initial RunState with a generated run_id.
        """
        run_id = str(uuid.uuid4())
        state = RunState(
            run_id=run_id,
            status=RunStatus.RUNNING,
            created_at=datetime.now(UTC).isoformat(),
        )
        async with self._lock:
            self._cleanup_expired()
            self._runs[run_id] = (state, time.monotonic())
        return state

    async def get_run(self, run_id: str) -> Optional[RunState]:
        """Get a run by ID.

        Returns None if the run doesn't exist or has expired.

        Args:
            run_id: The run identifier.

        Returns:
            The RunState if found and not expired, None otherwise.
        """
        async with self._lock:
            self._cleanup_expired()
            entry = self._runs.get(run_id)
            if entry is None:
                return None
            return entry[0]

    async def complete_run(self, run_id: str, result: AgentRunResponse) -> None:
        """Mark a run as completed with the given result.

        Args:
            run_id: The run identifier.
            result: The agent's response.
        """
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            state, _ = entry
            updated = RunState(
                run_id=state.run_id,
                status=RunStatus.COMPLETED,
                result=result,
                created_at=state.created_at,
            )
            self._runs[run_id] = (updated, time.monotonic())

    async def fail_run(self, run_id: str, result: AgentRunResponse) -> None:
        """Mark a run as failed with the given error response.

        Args:
            run_id: The run identifier.
            result: The error response.
        """
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            state, _ = entry
            updated = RunState(
                run_id=state.run_id,
                status=RunStatus.FAILED,
                result=result,
                created_at=state.created_at,
            )
            self._runs[run_id] = (updated, time.monotonic())

    async def list_runs(self) -> list[RunState]:
        """List all non-expired runs.

        Returns:
            List of active RunState objects.
        """
        async with self._lock:
            self._cleanup_expired()
            return [state for state, _ in self._runs.values()]

    def _cleanup_expired(self) -> None:
        """Remove expired entries. Called within the lock."""
        now = time.monotonic()
        expired = [
            rid
            for rid, (_, created) in self._runs.items()
            if now - created > self._expiry_seconds
        ]
        for rid in expired:
            del self._runs[rid]
