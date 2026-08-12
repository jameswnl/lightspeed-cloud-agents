"""Tests for the PostgreSQL run-state store.

Uses an in-memory mock of asyncpg since unit tests don't have a real database.
Tests verify the store's API contract and state transitions.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pytest_mock import MockerFixture


@pytest.fixture(name="mock_pool")
def mock_pool_fixture(mocker: MockerFixture) -> Any:
    """Create a mock asyncpg pool."""
    pool = mocker.AsyncMock()
    pool.execute = mocker.AsyncMock(return_value="INSERT 1")
    pool.fetchrow = mocker.AsyncMock(return_value=None)
    pool.fetch = mocker.AsyncMock(return_value=[])
    return pool


@pytest.fixture(name="store")
def store_fixture(mock_pool: Any) -> Any:
    """Create a RunStateStore with a mock pool (skips connect)."""
    from cloud_agents.storage.run_state_store import RunStateStore

    s = RunStateStore(db_url="postgresql://test:test@localhost/test")
    s._pool = mock_pool
    return s


class TestRunStateStoreCreate:
    """Tests for creating workflow state."""

    @pytest.mark.asyncio
    async def test_create_stores_initial_state(
        self, store: Any, mock_pool: Any
    ) -> None:
        """create() persists workflow state to the database."""
        await store.create(
            workflow_id="wf-1",
            workflow_name="test-workflow",
            definition={"spec": {"steps": []}},
            provider={"name": "openai", "model": "gpt-4o"},
            authz_context={"caller": "user:james"},
        )

        mock_pool.execute.assert_called()
        call_args = mock_pool.execute.call_args[0]
        assert "wf-1" in call_args
        assert "test-workflow" in call_args

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(
        self, store: Any, mock_pool: Any, mocker: MockerFixture
    ) -> None:
        """Duplicate workflow_id raises ValueError."""
        import asyncpg

        mock_pool.execute.side_effect = asyncpg.UniqueViolationError("")

        with pytest.raises(ValueError, match="already exists"):
            await store.create(
                workflow_id="wf-dup",
                workflow_name="test",
                definition={},
                provider={},
                authz_context={},
            )


class TestRunStateStoreGet:
    """Tests for retrieving workflow state."""

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, store: Any) -> None:
        """get() returns None for non-existent workflow."""
        result = await store.get("wf-nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_state(
        self, store: Any, mock_pool: Any
    ) -> None:
        """get() returns deserialized WorkflowState."""
        mock_pool.fetchrow.return_value = {
            "workflow_id": "wf-1",
            "workflow_name": "test",
            "status": "running",
            "current_step": "diagnose",
            "steps": '{"diagnosis": {"step_name": "diagnosis", "status": "running"}}',
            "events": '[{"type": "step.started", "step": "diagnose"}]',
            "definition": '{"spec": {}}',
            "provider": '{"name": "openai"}',
            "authz_context": '{"caller": "user:james"}',
            "workflow_context": "{}",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }

        result = await store.get("wf-1")
        assert result is not None
        assert result["workflow_id"] == "wf-1"
        assert result["status"] == "running"


class TestRunStateStoreUpdate:
    """Tests for updating step results and events."""

    @pytest.mark.asyncio
    async def test_update_step_persists(
        self, store: Any, mock_pool: Any
    ) -> None:
        """update_step() writes step result to database."""
        await store.update_step(
            workflow_id="wf-1",
            step_name="diagnose",
            status="completed",
            output={"summary": "all good"},
        )

        mock_pool.execute.assert_called()

    @pytest.mark.asyncio
    async def test_append_event(
        self, store: Any, mock_pool: Any
    ) -> None:
        """append_event() adds an event to the events array."""
        await store.append_event(
            workflow_id="wf-1",
            event={"type": "step.started", "step": "diagnose"},
        )

        mock_pool.execute.assert_called()


class TestRunStateStorePauseResume:
    """Tests for approval pause/resume."""

    @pytest.mark.asyncio
    async def test_set_paused(self, store: Any, mock_pool: Any) -> None:
        """set_paused() marks workflow as paused at a step."""
        await store.set_paused(
            workflow_id="wf-1",
            step_name="approve-fix",
        )

        mock_pool.execute.assert_called()

    @pytest.mark.asyncio
    async def test_resume(self, store: Any, mock_pool: Any) -> None:
        """resume() transitions workflow back to running."""
        await store.resume(workflow_id="wf-1")
        mock_pool.execute.assert_called()
        call_args = mock_pool.execute.call_args[0]
        assert "running" in call_args

    @pytest.mark.asyncio
    async def test_list_paused(self, store: Any, mock_pool: Any) -> None:
        """list_paused() returns workflow IDs paused at approval."""
        mock_pool.fetch.return_value = [
            {"workflow_id": "wf-1"},
            {"workflow_id": "wf-2"},
        ]

        result = await store.list_paused()
        assert result == ["wf-1", "wf-2"]


class TestRunStateStoreTerminal:
    """Tests for marking workflows terminal."""

    @pytest.mark.asyncio
    async def test_mark_terminal(self, store: Any, mock_pool: Any) -> None:
        """mark_terminal() sets final status."""
        await store.mark_terminal(
            workflow_id="wf-1",
            status="completed",
        )

        mock_pool.execute.assert_called()


class TestRunStateStoreFromEnv:
    """Tests for environment-based construction."""

    def test_from_env_returns_none_without_url(
        self, mocker: MockerFixture
    ) -> None:
        """Returns None when RUN_STATE_DB_URL is not set."""
        mocker.patch.dict(os.environ, {}, clear=True)

        from cloud_agents.storage.run_state_store import RunStateStore

        result = RunStateStore.from_env()
        assert result is None

    def test_from_env_returns_store_with_url(
        self, mocker: MockerFixture
    ) -> None:
        """Returns RunStateStore when RUN_STATE_DB_URL is set."""
        mocker.patch.dict(
            os.environ,
            {"RUN_STATE_DB_URL": "postgresql://localhost/test"},
            clear=False,
        )

        from cloud_agents.storage.run_state_store import RunStateStore

        result = RunStateStore.from_env()
        assert result is not None

    def test_no_temporal_imports(self) -> None:
        """run_state_store module has zero temporalio imports."""
        from cloud_agents.storage import run_state_store as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source
