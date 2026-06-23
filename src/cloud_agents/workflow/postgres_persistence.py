"""PostgreSQL-backed workflow state persistence.

Uses SQLAlchemy async engine with JSONB storage for workflow state.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import Column, JSON, String, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from agents.workflow.persistence import WorkflowPersistence
from agents.workflow.state import WorkflowState

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class WorkflowStateRow(Base):
    """Database row for workflow state."""

    __tablename__ = "workflow_states"

    workflow_id = Column(String, primary_key=True)
    workflow_name = Column(String, nullable=False)
    status = Column(String, nullable=False)
    # Using generic JSON (not PostgreSQL JSONB) for SQLite test compatibility.
    # Production gap: switch to JSONB for GIN index support on PostgreSQL.
    state_json = Column(JSON, nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class PostgresPersistence(WorkflowPersistence):
    """PostgreSQL-backed workflow state persistence.

    Attributes:
        engine: SQLAlchemy async engine.
    """

    def __init__(self, connection_string: str) -> None:
        """Initialize with a PostgreSQL connection string.

        Args:
            connection_string: Async PostgreSQL URL
                (e.g. postgresql+asyncpg://user:pass@host/db).
        """
        self._engine = create_async_engine(connection_string)
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def save(self, state: WorkflowState) -> None:
        """Upsert workflow state."""
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(WorkflowStateRow, state.workflow_id)
                if existing:
                    existing.workflow_name = state.workflow_name
                    existing.status = state.status
                    existing.state_json = state.model_dump(mode="json")
                    existing.updated_at = state.updated_at
                else:
                    row = WorkflowStateRow(
                        workflow_id=state.workflow_id,
                        workflow_name=state.workflow_name,
                        status=state.status,
                        state_json=state.model_dump(mode="json"),
                        created_at=state.created_at,
                        updated_at=state.updated_at,
                    )
                    session.add(row)

    async def load(self, workflow_id: str) -> Optional[WorkflowState]:
        """Load workflow state by ID."""
        async with self._session_factory() as session:
            row = await session.get(WorkflowStateRow, workflow_id)
            if row is None:
                return None
            return WorkflowState.model_validate(row.state_json)

    async def list_active(self) -> list[WorkflowState]:
        """List all active workflows."""
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT state_json FROM workflow_states WHERE status != 'completed'")
            )
            states = []
            for row in result.fetchall():
                data = row[0]
                if isinstance(data, str):
                    states.append(WorkflowState.model_validate_json(data))
                else:
                    states.append(WorkflowState.model_validate(data))
            return states

    async def delete(self, workflow_id: str) -> None:
        """Delete workflow state."""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(WorkflowStateRow, workflow_id)
                if row:
                    await session.delete(row)
