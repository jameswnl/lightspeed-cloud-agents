"""Unit tests for workflow state persistence."""

import tempfile

import pytest

from agents.workflow.persistence import FilePersistence, InMemoryPersistence
from agents.workflow.state import StepResult, WorkflowState


def _make_state(workflow_id: str = "w1") -> WorkflowState:
    return WorkflowState(
        workflow_id=workflow_id, workflow_name="test",
        created_at="2026-01-01", updated_at="2026-01-01",
        steps={"s1": StepResult(step_name="s1", status="completed")},
    )


class TestInMemoryPersistence:
    """Tests for in-memory persistence."""

    @pytest.mark.asyncio
    async def test_save_and_load(self) -> None:
        """Test save and load round-trip."""
        p = InMemoryPersistence()
        state = _make_state()
        await p.save(state)
        loaded = await p.load("w1")
        assert loaded.workflow_id == "w1"

    @pytest.mark.asyncio
    async def test_load_missing(self) -> None:
        """Test loading nonexistent returns None."""
        p = InMemoryPersistence()
        assert await p.load("missing") is None

    @pytest.mark.asyncio
    async def test_list_active(self) -> None:
        """Test listing active workflows."""
        p = InMemoryPersistence()
        await p.save(_make_state("w1"))
        await p.save(_make_state("w2"))
        active = await p.list_active()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        """Test deleting a workflow."""
        p = InMemoryPersistence()
        await p.save(_make_state())
        await p.delete("w1")
        assert await p.load("w1") is None


class TestFilePersistence:
    """Tests for file-based persistence."""

    @pytest.mark.asyncio
    async def test_save_and_load(self) -> None:
        """Test save and load from file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            state = _make_state()
            await p.save(state)
            loaded = await p.load("w1")
            assert loaded.workflow_id == "w1"
            assert loaded.steps["s1"].status == "completed"

    @pytest.mark.asyncio
    async def test_load_missing(self) -> None:
        """Test loading nonexistent file returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            assert await p.load("missing") is None

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        """Test deleting a workflow file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = FilePersistence(tmpdir)
            await p.save(_make_state())
            await p.delete("w1")
            assert await p.load("w1") is None
