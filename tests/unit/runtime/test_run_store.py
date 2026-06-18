"""Unit tests for RunStore."""

import asyncio
from unittest.mock import patch

import pytest

from agents.models import AgentRunResponse, RunState, RunStatus
from agents.runtime.run_store import RunStore


@pytest.fixture(name="store")
def store_fixture() -> RunStore:
    """Create a fresh RunStore for each test."""
    return RunStore(expiry_seconds=3600)


class TestRunStore:
    """Tests for the in-memory RunStore."""

    @pytest.mark.asyncio
    async def test_create_run(self, store: RunStore) -> None:
        """Test creating a new run returns a RunState with running status."""
        state = await store.create_run()
        assert state.status == RunStatus.RUNNING
        assert state.result is None
        assert state.run_id

    @pytest.mark.asyncio
    async def test_get_run(self, store: RunStore) -> None:
        """Test getting a run by ID."""
        state = await store.create_run()
        retrieved = await store.get_run(state.run_id)
        assert retrieved is not None
        assert retrieved.run_id == state.run_id

    @pytest.mark.asyncio
    async def test_get_unknown_run_returns_none(self, store: RunStore) -> None:
        """Test getting a nonexistent run returns None."""
        result = await store.get_run("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_run(self, store: RunStore) -> None:
        """Test completing a run stores the result."""
        state = await store.create_run()
        response = AgentRunResponse(
            output={"summary": "done"},
            output_type="DiagnosticReport",
            usage={"input_tokens": 10, "output_tokens": 20},
            agent_name="test",
            success=True,
        )
        await store.complete_run(state.run_id, response)
        retrieved = await store.get_run(state.run_id)
        assert retrieved.status == RunStatus.COMPLETED
        assert retrieved.result.success is True

    @pytest.mark.asyncio
    async def test_fail_run(self, store: RunStore) -> None:
        """Test failing a run stores the error."""
        state = await store.create_run()
        response = AgentRunResponse(
            output={},
            output_type="error",
            usage={"input_tokens": 0, "output_tokens": 0},
            agent_name="test",
            success=False,
            error="Something broke",
        )
        await store.fail_run(state.run_id, response)
        retrieved = await store.get_run(state.run_id)
        assert retrieved.status == RunStatus.FAILED
        assert retrieved.result.success is False

    @pytest.mark.asyncio
    async def test_expired_run_returns_none(self) -> None:
        """Test that expired runs are cleaned up on access."""
        store = RunStore(expiry_seconds=0)
        state = await store.create_run()
        await asyncio.sleep(0.01)
        result = await store.get_run(state.run_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_runs(self, store: RunStore) -> None:
        """Test listing all active runs."""
        await store.create_run()
        await store.create_run()
        runs = await store.list_runs()
        assert len(runs) == 2
