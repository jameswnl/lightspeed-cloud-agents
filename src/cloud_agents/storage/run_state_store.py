"""PostgreSQL-backed run-state store for workflow execution state.

Persists the full workflow state needed for API parity between
Temporal and local executors: step results, events, authz context,
workflow context, and approval pause/resume payloads.

Schema is auto-migrated on connect() via CREATE TABLE IF NOT EXISTS.
Follows the same pattern as TranscriptStore (asyncpg, from_env factory).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_run_state (
    workflow_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    current_step TEXT,
    steps JSONB NOT NULL DEFAULT '{}',
    events JSONB NOT NULL DEFAULT '[]',
    definition JSONB NOT NULL DEFAULT '{}',
    provider JSONB NOT NULL DEFAULT '{}',
    authz_context JSONB NOT NULL DEFAULT '{}',
    workflow_context JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_workflow_run_state_status
    ON workflow_run_state(status);
"""

_INSERT_SQL = """
INSERT INTO workflow_run_state
    (workflow_id, workflow_name, status, definition, provider, authz_context,
     user_id, session_id, parent_workflow_id, created_at, updated_at)
VALUES ($1, $2, 'running', $3::jsonb, $4::jsonb, $5::jsonb,
        $6, $7, $8, $9, $9);
"""

_SELECT_SQL = """
SELECT workflow_id, workflow_name, status, current_step, steps, events,
       definition, provider, authz_context, workflow_context,
       user_id, session_id, parent_workflow_id,
       created_at, updated_at
FROM workflow_run_state
WHERE workflow_id = $1;
"""

_LIST_BY_USER_SQL = """
SELECT workflow_id
FROM workflow_run_state
WHERE user_id = $1
ORDER BY created_at DESC;
"""

_LIST_BY_SESSION_SQL = """
SELECT workflow_id
FROM workflow_run_state
WHERE session_id = $1
ORDER BY created_at DESC;
"""

_UPDATE_STEP_SQL = """
UPDATE workflow_run_state
SET steps = jsonb_set(
        COALESCE(steps, '{}'),
        ARRAY[$2::text],
        $3::jsonb
    ),
    current_step = $2,
    updated_at = $4
WHERE workflow_id = $1;
"""

_APPEND_EVENT_SQL = """
UPDATE workflow_run_state
SET events = COALESCE(events, '[]'::jsonb) || $2::jsonb,
    updated_at = $3
WHERE workflow_id = $1;
"""

_SET_STATUS_SQL = """
UPDATE workflow_run_state
SET status = $2,
    current_step = $3,
    updated_at = $4
WHERE workflow_id = $1;
"""

_UPDATE_CONTEXT_SQL = """
UPDATE workflow_run_state
SET workflow_context = $2::jsonb,
    updated_at = $3
WHERE workflow_id = $1;
"""

_LIST_PAUSED_SQL = """
SELECT workflow_id
FROM workflow_run_state
WHERE status = 'paused'
ORDER BY updated_at;
"""

_DELETE_SQL = """
DELETE FROM workflow_run_state WHERE workflow_id = $1;
"""


class RunStateStore:
    """Async PostgreSQL store for workflow execution state.

    Attributes:
        _db_url: PostgreSQL connection URL.
        _pool: asyncpg connection pool (None until connect()).
    """

    def __init__(self, db_url: str) -> None:
        """Initialize the run-state store.

        Parameters:
            db_url: PostgreSQL connection URL.
        """
        self._db_url = db_url
        self._pool: Optional[asyncpg.Pool] = None

    @classmethod
    def from_env(cls) -> Optional[RunStateStore]:
        """Create a RunStateStore from environment variables.

        Reads RUN_STATE_DB_URL. Returns None when not set.

        Returns:
            RunStateStore instance or None if not configured.
        """
        db_url = os.environ.get("RUN_STATE_DB_URL", "")
        if not db_url:
            return None
        return cls(db_url=db_url)

    async def connect(self) -> None:
        """Connect to PostgreSQL and run schema migration.

        Runs Alembic migrations if available, then falls back to
        CREATE TABLE IF NOT EXISTS for the base schema.
        """
        self._run_alembic()
        self._pool = await asyncpg.create_pool(self._db_url)
        await self._pool.execute(_SCHEMA_SQL)
        await self._pool.execute(_INDEX_SQL)
        logger.info("RunStateStore connected to PostgreSQL")

    def _run_alembic(self) -> None:
        """Run Alembic migrations if the alembic package is available."""
        try:
            from alembic import command
            from alembic.config import Config

            cfg = Config("alembic.ini")
            cfg.set_main_option("sqlalchemy.url", self._db_url)
            command.upgrade(cfg, "head")
            logger.info("Alembic migrations applied")
        except Exception as exc:
            logger.debug("Alembic migration skipped: %s", exc)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            logger.info("RunStateStore connection pool closed")

    def _ensure_connected(self) -> asyncpg.Pool:
        """Return the pool or raise if not connected."""
        if self._pool is None:
            raise RuntimeError("RunStateStore not connected — call connect() first")
        return self._pool

    @staticmethod
    def _check_updated(result: str, workflow_id: str) -> None:
        """Raise KeyError if no rows were affected by an UPDATE."""
        if result == "UPDATE 0":
            raise KeyError(f"Workflow '{workflow_id}' not found")

    async def create(
        self,
        workflow_id: str,
        workflow_name: str,
        definition: dict[str, Any],
        provider: dict[str, Any],
        authz_context: dict[str, Any],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_workflow_id: Optional[str] = None,
    ) -> None:
        """Create a new workflow run state.

        Parameters:
            workflow_id: Unique workflow execution ID.
            workflow_name: Name from workflow definition.
            definition: Full workflow definition snapshot.
            provider: Provider configuration.
            authz_context: Authorization context from caller.
            user_id: Identity of the user who initiated this workflow.
            session_id: Groups related workflows (caller-provided).
            parent_workflow_id: Parent workflow ID for sub-workflows.

        Raises:
            ValueError: If workflow_id already exists.
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        try:
            await pool.execute(
                _INSERT_SQL,
                workflow_id,
                workflow_name,
                json.dumps(definition),
                json.dumps(provider),
                json.dumps(authz_context),
                user_id,
                session_id,
                parent_workflow_id,
                now,
            )
        except asyncpg.UniqueViolationError:
            raise ValueError(f"Workflow '{workflow_id}' already exists") from None

        logger.debug("Created run state for workflow=%s", workflow_id)

    async def get(self, workflow_id: str) -> Optional[dict[str, Any]]:
        """Retrieve the full workflow run state.

        Parameters:
            workflow_id: Workflow execution ID.

        Returns:
            Dict with all state fields, or None if not found.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        row = await pool.fetchrow(_SELECT_SQL, workflow_id)
        if row is None:
            return None

        def _parse_json(val: Any) -> Any:
            if isinstance(val, str):
                return json.loads(val)
            return val

        return {
            "workflow_id": row["workflow_id"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "current_step": row["current_step"],
            "steps": _parse_json(row["steps"]),
            "events": _parse_json(row["events"]),
            "definition": _parse_json(row["definition"]),
            "provider": _parse_json(row["provider"]),
            "authz_context": _parse_json(row["authz_context"]),
            "workflow_context": _parse_json(row["workflow_context"]),
            "user_id": row["user_id"],
            "session_id": row["session_id"],
            "parent_workflow_id": row["parent_workflow_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def update_step(
        self,
        workflow_id: str,
        step_name: str,
        status: str,
        output: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update a step's result in the workflow state.

        Parameters:
            workflow_id: Workflow execution ID.
            step_name: Step output key.
            status: Step status (running, completed, failed, etc.).
            output: Step output data.
            error: Error message if failed.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        step_data = {
            "step_name": step_name,
            "status": status,
            "output": output,
            "error": error,
        }
        result = await pool.execute(
            _UPDATE_STEP_SQL,
            workflow_id,
            step_name,
            json.dumps(step_data),
            now,
        )
        self._check_updated(result, workflow_id)

    async def append_event(
        self,
        workflow_id: str,
        event: dict[str, Any],
    ) -> None:
        """Append a lifecycle event to the workflow's event list.

        Parameters:
            workflow_id: Workflow execution ID.
            event: Event dict (type, step, timestamp, etc.).

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        if "timestamp" not in event:
            event["timestamp"] = now.isoformat()
        result = await pool.execute(
            _APPEND_EVENT_SQL,
            workflow_id,
            json.dumps([event]),
            now,
        )
        self._check_updated(result, workflow_id)

    async def set_paused(
        self,
        workflow_id: str,
        step_name: str,
    ) -> None:
        """Mark a workflow as paused at an approval step.

        Parameters:
            workflow_id: Workflow execution ID.
            step_name: The approval step waiting for a signal.

        Raises:
            KeyError: If workflow does not exist.
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        result = await pool.execute(_SET_STATUS_SQL, workflow_id, "paused", step_name, now)
        self._check_updated(result, workflow_id)

    async def resume(
        self,
        workflow_id: str,
    ) -> None:
        """Resume a paused workflow after approval.

        Parameters:
            workflow_id: Workflow execution ID.

        Raises:
            KeyError: If workflow does not exist.
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        result = await pool.execute(_SET_STATUS_SQL, workflow_id, "running", None, now)
        self._check_updated(result, workflow_id)

    _TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

    async def mark_terminal(
        self,
        workflow_id: str,
        status: str,
    ) -> None:
        """Mark a workflow as terminal (completed, failed, cancelled).

        Parameters:
            workflow_id: Workflow execution ID.
            status: Terminal status string.

        Raises:
            ValueError: If status is not a terminal status.
            KeyError: If workflow does not exist.
            RuntimeError: If not connected.
        """
        if status not in self._TERMINAL_STATUSES:
            raise ValueError(f"'{status}' is not a terminal status")
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        result = await pool.execute(_SET_STATUS_SQL, workflow_id, status, None, now)
        self._check_updated(result, workflow_id)

    async def update_workflow_context(
        self,
        workflow_id: str,
        context: dict[str, Any],
    ) -> None:
        """Update the workflow context (for escalation handoff).

        Parameters:
            workflow_id: Workflow execution ID.
            context: Workflow context dict.

        Raises:
            KeyError: If workflow does not exist.
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        now = datetime.now(UTC)
        result = await pool.execute(
            _UPDATE_CONTEXT_SQL,
            workflow_id,
            json.dumps(context),
            now,
        )
        self._check_updated(result, workflow_id)

    async def list_paused(self) -> list[str]:
        """List workflow IDs that are paused at approval steps.

        Used on startup to resume orphaned paused workflows.

        Returns:
            List of workflow IDs with status='paused'.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        rows = await pool.fetch(_LIST_PAUSED_SQL)
        return [row["workflow_id"] for row in rows]

    async def list_by_user(self, user_id: str) -> list[str]:
        """List workflow IDs belonging to a user.

        Parameters:
            user_id: User identity string.

        Returns:
            List of workflow IDs ordered by creation time (newest first).

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        rows = await pool.fetch(_LIST_BY_USER_SQL, user_id)
        return [row["workflow_id"] for row in rows]

    async def list_by_session(self, session_id: str) -> list[str]:
        """List workflow IDs within a session.

        Parameters:
            session_id: Session identifier (caller-provided).

        Returns:
            List of workflow IDs ordered by creation time.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        rows = await pool.fetch(_LIST_BY_SESSION_SQL, session_id)
        return [row["workflow_id"] for row in rows]

    async def delete(self, workflow_id: str) -> None:
        """Delete a workflow's run state.

        Parameters:
            workflow_id: Workflow execution ID.

        Raises:
            RuntimeError: If not connected.
        """
        pool = self._ensure_connected()
        await pool.execute(_DELETE_SQL, workflow_id)
