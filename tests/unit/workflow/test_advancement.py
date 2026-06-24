"""Unit tests for workflow advancement and recovery."""

from __future__ import annotations

import pytest

from agents.workflow.advancement import RecoveryPoller, StaleStateError, save_with_version
from agents.workflow.persistence import InMemoryPersistence
from agents.workflow.state import StepResult, WorkflowState


def _make_state(
    workflow_id: str = "wf-1",
    status: str = "running",
    version: int = 1,
) -> WorkflowState:
    """Create a test workflow state."""
    return WorkflowState(
        workflow_id=workflow_id, workflow_name="test",
        status=status, version=version,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


class TestSaveWithVersion:
    """Tests for optimistic locking save."""

    @pytest.mark.asyncio
    async def test_save_increments_version(self) -> None:
        """Test that save increments the version."""
        p = InMemoryPersistence()
        state = _make_state(version=1)
        await p.save(state)

        await save_with_version(p, state, expected_version=1)
        assert state.version == 2

    @pytest.mark.asyncio
    async def test_stale_version_raises(self) -> None:
        """Test that version mismatch raises StaleStateError."""
        p = InMemoryPersistence()
        state = _make_state(version=3)
        await p.save(state)

        with pytest.raises(StaleStateError, match="version mismatch"):
            await save_with_version(p, state, expected_version=1)

    @pytest.mark.asyncio
    async def test_new_workflow_saves(self) -> None:
        """Test saving a new workflow (not in persistence yet)."""
        p = InMemoryPersistence()
        state = _make_state(version=1)
        await save_with_version(p, state, expected_version=1)
        assert state.version == 2


class TestRecoveryPoller:
    """Tests for orphaned step recovery."""

    @pytest.mark.asyncio
    async def test_detects_orphaned_step(self) -> None:
        """Test that orphaned dispatched steps are marked failed."""
        p = InMemoryPersistence()
        state = _make_state()
        state.steps["r1"] = StepResult(
            step_name="s1", status="dispatched",
            started_at="2020-01-01T00:00:00Z",
        )
        await p.save(state)

        poller = RecoveryPoller(p, step_timeout=10)
        await poller._poll_once()

        updated = await p.load("wf-1")
        assert updated.steps["r1"].status == "failed"
        assert "timed out" in updated.steps["r1"].error
        assert updated.status == "failed"

    @pytest.mark.asyncio
    async def test_ignores_recent_dispatched(self) -> None:
        """Test that recently dispatched steps are not marked failed."""
        from datetime import datetime, timezone
        p = InMemoryPersistence()
        state = _make_state()
        state.steps["r1"] = StepResult(
            step_name="s1", status="dispatched",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        await p.save(state)

        poller = RecoveryPoller(p, step_timeout=600)
        await poller._poll_once()

        updated = await p.load("wf-1")
        assert updated.steps["r1"].status == "dispatched"

    @pytest.mark.asyncio
    async def test_ignores_completed_workflows(self) -> None:
        """Test that completed workflows are skipped."""
        p = InMemoryPersistence()
        state = _make_state(status="completed")
        await p.save(state)

        poller = RecoveryPoller(p, step_timeout=1)
        await poller._poll_once()
