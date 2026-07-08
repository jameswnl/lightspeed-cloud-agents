"""PostgreSQL-backed transcript store for full agent transcripts.

Stores untruncated step transcripts in PostgreSQL so they survive
sandbox container destruction. Uses asyncpg for async database access.

Schema is auto-migrated on connect() via CREATE TABLE IF NOT EXISTS.
Retention is enforced by cleanup_expired() using TRANSCRIPT_RETENTION_DAYS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import asyncpg

from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS step_transcripts (
    id SERIAL PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    events JSONB NOT NULL,
    cost_usd DOUBLE PRECISION,
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(workflow_id, step_name)
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_step_transcripts_workflow
    ON step_transcripts(workflow_id);
"""

_UPSERT_SQL = """
INSERT INTO step_transcripts
    (workflow_id, step_name, events, cost_usd, input_tokens, output_tokens, duration_ms)
VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)
ON CONFLICT (workflow_id, step_name)
DO UPDATE SET
    events = EXCLUDED.events,
    cost_usd = EXCLUDED.cost_usd,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    duration_ms = EXCLUDED.duration_ms,
    created_at = NOW();
"""

_SELECT_SQL = """
SELECT events, cost_usd, input_tokens, output_tokens, duration_ms
FROM step_transcripts
WHERE workflow_id = $1 AND step_name = $2;
"""

_LIST_STEPS_SQL = """
SELECT step_name FROM step_transcripts
WHERE workflow_id = $1
ORDER BY created_at;
"""

_DELETE_WORKFLOW_SQL = """
DELETE FROM step_transcripts WHERE workflow_id = $1;
"""

_CLEANUP_SQL = """
DELETE FROM step_transcripts
WHERE created_at < NOW() - CAST($1 || ' days' AS INTERVAL);
"""


class TranscriptStore:
    """Async PostgreSQL store for full agent step transcripts.

    Attributes:
        _db_url: PostgreSQL connection URL.
        _retention_days: Number of days to retain transcripts.
        _pool: asyncpg connection pool (None until connect() is called).
    """

    def __init__(
        self,
        db_url: str,
        retention_days: int = 30,
    ) -> None:
        """Initialize the transcript store.

        Parameters:
            db_url: PostgreSQL connection URL.
            retention_days: Days to retain transcripts before cleanup.
        """
        self._db_url = db_url
        self._retention_days = retention_days
        self._pool: Optional[asyncpg.Pool] = None

    @classmethod
    def from_env(cls) -> Optional[TranscriptStore]:
        """Create a TranscriptStore from environment variables.

        Reads TRANSCRIPT_DB_URL and TRANSCRIPT_RETENTION_DAYS.
        Returns None when TRANSCRIPT_DB_URL is not set.

        Returns:
            TranscriptStore instance or None if not configured.
        """
        db_url = os.environ.get("TRANSCRIPT_DB_URL", "")
        if not db_url:
            return None

        retention_days = int(os.environ.get("TRANSCRIPT_RETENTION_DAYS", "30"))
        return cls(db_url=db_url, retention_days=retention_days)

    async def connect(self) -> None:
        """Connect to PostgreSQL and run schema migration.

        Creates the connection pool and ensures the step_transcripts
        table exists via CREATE TABLE IF NOT EXISTS.
        """
        self._pool = await asyncpg.create_pool(self._db_url)
        await self._pool.execute(_SCHEMA_SQL)
        await self._pool.execute(_INDEX_SQL)
        logger.info("TranscriptStore connected to PostgreSQL")

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            logger.info("TranscriptStore connection pool closed")

    def _ensure_connected(self) -> asyncpg.Pool:
        """Return the pool or raise if not connected.

        Returns:
            The asyncpg connection pool.

        Raises:
            RuntimeError: If connect() has not been called.
        """
        if self._pool is None:
            raise RuntimeError("TranscriptStore not connected — call connect() first")
        return self._pool

    async def save(
        self,
        workflow_id: str,
        step_name: str,
        transcript: StepTranscript,
    ) -> None:
        """Save or update a step transcript.

        Uses INSERT ... ON CONFLICT ... DO UPDATE for idempotent upserts.

        Parameters:
            workflow_id: Workflow execution ID.
            step_name: Step output key.
            transcript: Full step transcript to persist.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        events_json = json.dumps([e.model_dump() for e in transcript.events])
        await pool.execute(
            _UPSERT_SQL,
            workflow_id,
            step_name,
            events_json,
            transcript.cost_usd,
            transcript.input_tokens,
            transcript.output_tokens,
            transcript.duration_ms,
        )
        logger.debug(
            "Saved transcript for workflow=%s step=%s (%d events)",
            workflow_id,
            step_name,
            len(transcript.events),
        )

    async def get(
        self,
        workflow_id: str,
        step_name: str,
    ) -> Optional[StepTranscript]:
        """Retrieve a step transcript.

        Parameters:
            workflow_id: Workflow execution ID.
            step_name: Step output key.

        Returns:
            StepTranscript if found, None otherwise.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        row = await pool.fetchrow(_SELECT_SQL, workflow_id, step_name)
        if row is None:
            return None

        events_data = row["events"]
        if isinstance(events_data, str):
            events_data = json.loads(events_data)

        events = [
            TranscriptEvent(
                ts=e.get("ts", ""),
                type=e.get("type", "result"),
                data=e.get("data", {}),
            )
            for e in events_data
        ]

        return StepTranscript(
            step_name=step_name,
            events=events,
            cost_usd=row["cost_usd"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            duration_ms=row["duration_ms"],
        )

    async def list_steps(self, workflow_id: str) -> list[str]:
        """List step names that have transcripts for a workflow.

        Parameters:
            workflow_id: Workflow execution ID.

        Returns:
            List of step names with stored transcripts.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        rows = await pool.fetch(_LIST_STEPS_SQL, workflow_id)
        return [row["step_name"] for row in rows]

    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete all transcripts for a workflow.

        Parameters:
            workflow_id: Workflow execution ID.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        await pool.execute(_DELETE_WORKFLOW_SQL, workflow_id)
        logger.debug("Deleted transcripts for workflow=%s", workflow_id)

    async def cleanup_expired(self) -> int:
        """Delete transcripts older than retention_days.

        Returns:
            Number of deleted rows.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        result = await pool.execute(_CLEANUP_SQL, str(self._retention_days))
        # asyncpg returns "DELETE N" as result string
        match = re.search(r"\d+", result)
        return int(match.group()) if match else 0
