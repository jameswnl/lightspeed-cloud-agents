"""Unit tests for PostgresPersistence using SQLite async as stand-in.

Tests the SQLAlchemy persistence logic without requiring a real PostgreSQL.
SQLite async provides the same async session interface.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from agents.workflow.postgres_persistence import Base, PostgresPersistence, WorkflowStateRow
from agents.workflow.state import StepResult, WorkflowState


@pytest.fixture
async def persistence(tmp_path):
    """Create a PostgresPersistence backed by SQLite for testing."""
    db_path = tmp_path / "test.db"
    conn_str = f"sqlite+aiosqlite:///{db_path}"
    p = PostgresPersistence(conn_str)
    await p.initialize()
    yield p


def _make_state(workflow_id: str = "w1", status: str = "running") -> WorkflowState:
    return WorkflowState(
        workflow_id=workflow_id,
        workflow_name="test-workflow",
        status=status,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        steps={"s1": StepResult(step_name="s1", status="completed")},
    )


class TestPostgresPersistence:
    """Tests for PostgresPersistence (using SQLite async)."""

    @pytest.mark.asyncio
    async def test_save_and_load(self, persistence) -> None:
        """Test save and load round-trip."""
        state = _make_state()
        await persistence.save(state)
        loaded = await persistence.load("w1")
        assert loaded is not None
        assert loaded.workflow_id == "w1"
        assert loaded.workflow_name == "test-workflow"
        assert loaded.steps["s1"].status == "completed"

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, persistence) -> None:
        """Test loading nonexistent returns None."""
        assert await persistence.load("missing") is None

    @pytest.mark.asyncio
    async def test_update_existing(self, persistence) -> None:
        """Test that saving an existing workflow updates it."""
        state = _make_state()
        await persistence.save(state)

        state.status = "completed"
        state.updated_at = "2026-01-02T00:00:00+00:00"
        await persistence.save(state)

        loaded = await persistence.load("w1")
        assert loaded.status == "completed"

    @pytest.mark.asyncio
    async def test_list_active(self, persistence) -> None:
        """Test listing active (non-completed) workflows."""
        await persistence.save(_make_state("w1", "running"))
        await persistence.save(_make_state("w2", "paused"))
        await persistence.save(_make_state("w3", "completed"))

        active = await persistence.list_active()
        assert len(active) == 2
        ids = {s.workflow_id for s in active}
        assert ids == {"w1", "w2"}

    @pytest.mark.asyncio
    async def test_delete(self, persistence) -> None:
        """Test deleting a workflow."""
        await persistence.save(_make_state())
        await persistence.delete("w1")
        assert await persistence.load("w1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, persistence) -> None:
        """Test deleting a nonexistent workflow doesn't error."""
        await persistence.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_state_preserves_nested_steps(self, persistence) -> None:
        """Test that nested step results survive serialization."""
        state = _make_state()
        state.steps["s2"] = StepResult(
            step_name="s2",
            status="failed",
            error="Something broke",
            output={"detail": "connection refused"},
        )
        await persistence.save(state)
        loaded = await persistence.load("w1")
        assert loaded.steps["s2"].error == "Something broke"
        assert loaded.steps["s2"].output["detail"] == "connection refused"
