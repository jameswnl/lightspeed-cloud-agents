"""Tests for identity-related additions to RunStateStore and TranscriptStore.

Tests cover:
- RunStateStore: create with identity fields, list_by_user, list_by_session
- TranscriptStore: save with trace_id/messages, load_recent_turns
- Updated _SCHEMA_SQL includes new columns
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest


# ── RunStateStore identity tests ──────────────────────────


class TestRunStateStoreIdentityCreate:
    """Tests for RunStateStore.create() with identity fields."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value="INSERT 1")
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a RunStateStore with a mock pool."""
        from cloud_agents.storage.run_state_store import RunStateStore

        s = RunStateStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_create_with_identity_fields(self, store: Any, mock_pool: Any) -> None:
        """create() accepts and persists identity fields."""
        await store.create(
            workflow_id="wf-1",
            workflow_name="test-workflow",
            definition={"spec": {}},
            provider={"name": "openai"},
            authz_context={"caller": "user:james"},
            user_id="user-42",
            session_id="sess-abc",
            parent_workflow_id="wf-parent",
        )
        mock_pool.execute.assert_called()
        call_args = mock_pool.execute.call_args[0]
        assert "wf-1" in call_args
        assert "user-42" in call_args
        assert "sess-abc" in call_args
        assert "wf-parent" in call_args

    @pytest.mark.asyncio
    async def test_create_without_identity_fields(self, store: Any, mock_pool: Any) -> None:
        """create() works without identity fields (backward compat)."""
        await store.create(
            workflow_id="wf-1",
            workflow_name="test-workflow",
            definition={"spec": {}},
            provider={"name": "openai"},
            authz_context={},
        )
        mock_pool.execute.assert_called()


class TestRunStateStoreListByUser:
    """Tests for RunStateStore.list_by_user()."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value="INSERT 1")
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a RunStateStore with a mock pool."""
        from cloud_agents.storage.run_state_store import RunStateStore

        s = RunStateStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_list_by_user_returns_workflow_ids(self, store: Any, mock_pool: Any) -> None:
        """list_by_user() returns workflow IDs for a user."""
        mock_pool.fetch.return_value = [
            {"workflow_id": "wf-1"},
            {"workflow_id": "wf-2"},
        ]
        result = await store.list_by_user("user-42")
        assert result == ["wf-1", "wf-2"]
        # Verify the query was called with user_id parameter
        call_args = mock_pool.fetch.call_args[0]
        assert "user-42" in call_args

    @pytest.mark.asyncio
    async def test_list_by_user_returns_empty_for_unknown(self, store: Any, mock_pool: Any) -> None:
        """list_by_user() returns empty list for unknown user."""
        mock_pool.fetch.return_value = []
        result = await store.list_by_user("user-unknown")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_by_user_not_connected_raises(self) -> None:
        """list_by_user() raises RuntimeError when not connected."""
        from cloud_agents.storage.run_state_store import RunStateStore

        store = RunStateStore(db_url="postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="not connected"):
            await store.list_by_user("user-42")


class TestRunStateStoreListBySession:
    """Tests for RunStateStore.list_by_session()."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value="INSERT 1")
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a RunStateStore with a mock pool."""
        from cloud_agents.storage.run_state_store import RunStateStore

        s = RunStateStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_list_by_session_returns_workflow_ids(self, store: Any, mock_pool: Any) -> None:
        """list_by_session() returns workflow IDs for a session."""
        mock_pool.fetch.return_value = [
            {"workflow_id": "wf-3"},
            {"workflow_id": "wf-4"},
        ]
        result = await store.list_by_session("sess-abc")
        assert result == ["wf-3", "wf-4"]
        call_args = mock_pool.fetch.call_args[0]
        assert "sess-abc" in call_args

    @pytest.mark.asyncio
    async def test_list_by_session_returns_empty(self, store: Any, mock_pool: Any) -> None:
        """list_by_session() returns empty for unknown session."""
        mock_pool.fetch.return_value = []
        result = await store.list_by_session("sess-unknown")
        assert result == []


class TestRunStateStoreGetIncludesIdentity:
    """Tests that get() returns identity fields."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value="INSERT 1")
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a RunStateStore with a mock pool."""
        from cloud_agents.storage.run_state_store import RunStateStore

        s = RunStateStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_get_returns_identity_fields(self, store: Any, mock_pool: Any) -> None:
        """get() includes user_id, session_id, parent_workflow_id."""
        mock_pool.fetchrow.return_value = {
            "workflow_id": "wf-1",
            "workflow_name": "test",
            "status": "running",
            "current_step": None,
            "steps": "{}",
            "events": "[]",
            "definition": "{}",
            "provider": "{}",
            "authz_context": "{}",
            "workflow_context": "{}",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "user_id": "user-42",
            "session_id": "sess-abc",
            "parent_workflow_id": "wf-parent",
        }
        result = await store.get("wf-1")
        assert result is not None
        assert result["user_id"] == "user-42"
        assert result["session_id"] == "sess-abc"
        assert result["parent_workflow_id"] == "wf-parent"


class TestRunStateStoreSchemaIncludesIdentity:
    """Tests that _SCHEMA_SQL includes new columns."""

    def test_schema_includes_user_id(self) -> None:
        """_SCHEMA_SQL includes user_id column."""
        from cloud_agents.storage.run_state_store import _SCHEMA_SQL

        assert "user_id" in _SCHEMA_SQL

    def test_schema_includes_session_id(self) -> None:
        """_SCHEMA_SQL includes session_id column."""
        from cloud_agents.storage.run_state_store import _SCHEMA_SQL

        assert "session_id" in _SCHEMA_SQL

    def test_schema_includes_parent_workflow_id(self) -> None:
        """_SCHEMA_SQL includes parent_workflow_id column."""
        from cloud_agents.storage.run_state_store import _SCHEMA_SQL

        assert "parent_workflow_id" in _SCHEMA_SQL


# ── TranscriptStore identity tests ────────────────────────


class TestTranscriptStoreSaveWithIdentity:
    """Tests for TranscriptStore.save() with trace_id and messages."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a TranscriptStore with a mock pool."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        s = TranscriptStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_save_with_trace_id_and_messages(self, store: Any, mock_pool: Any) -> None:
        """save() accepts and persists trace_id and messages."""
        from cloud_agents.workflow.core.models import StepTranscript, TranscriptEvent

        transcript = StepTranscript(
            step_name="diagnose",
            events=[
                TranscriptEvent(ts="2026-01-01T00:00:00Z", type="result", data={}),
            ],
        )
        messages = [
            {"role": "user", "content": "What is wrong?"},
            {"role": "assistant", "content": "The pod is in CrashLoopBackOff."},
        ]
        await store.save(
            workflow_id="wf-1",
            step_name="diagnose",
            transcript=transcript,
            trace_id="trace-xyz",
            messages=messages,
        )
        mock_pool.execute.assert_called()
        # Verify trace_id and messages are in the call args
        call_args = mock_pool.execute.call_args[0]
        assert "trace-xyz" in call_args
        # messages should be JSON serialized
        messages_json = json.dumps(messages)
        assert messages_json in call_args

    @pytest.mark.asyncio
    async def test_save_without_identity_fields(self, store: Any, mock_pool: Any) -> None:
        """save() works without trace_id and messages (backward compat)."""
        from cloud_agents.workflow.core.models import StepTranscript, TranscriptEvent

        transcript = StepTranscript(
            step_name="diagnose",
            events=[
                TranscriptEvent(ts="2026-01-01T00:00:00Z", type="result", data={}),
            ],
        )
        await store.save(
            workflow_id="wf-1",
            step_name="diagnose",
            transcript=transcript,
        )
        mock_pool.execute.assert_called()


class TestTranscriptStoreLoadRecentTurns:
    """Tests for TranscriptStore.load_recent_turns()."""

    @pytest.fixture(name="mock_pool")
    def mock_pool_fixture(self) -> Any:
        """Create a mock asyncpg pool."""
        pool = AsyncMock()
        pool.execute = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        pool.fetch = AsyncMock(return_value=[])
        return pool

    @pytest.fixture(name="store")
    def store_fixture(self, mock_pool: Any) -> Any:
        """Create a TranscriptStore with a mock pool."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        s = TranscriptStore(db_url="postgresql://test:test@localhost/test")
        s._pool = mock_pool
        return s

    @pytest.mark.asyncio
    async def test_load_recent_turns_returns_messages(self, store: Any, mock_pool: Any) -> None:
        """load_recent_turns() returns messages from recent steps."""
        messages_1 = [
            {"role": "user", "content": "What is wrong?"},
            {"role": "assistant", "content": "Checking pods..."},
        ]
        messages_2 = [
            {"role": "user", "content": "Fix it"},
            {"role": "assistant", "content": "Applied patch."},
        ]
        mock_pool.fetch.return_value = [
            {"step_name": "diagnose", "messages": json.dumps(messages_1)},
            {"step_name": "fix", "messages": json.dumps(messages_2)},
        ]
        result = await store.load_recent_turns("wf-1", limit=10)
        assert len(result) == 2
        assert result[0]["step_name"] == "diagnose"
        assert result[0]["messages"] == messages_1
        assert result[1]["step_name"] == "fix"
        assert result[1]["messages"] == messages_2

    @pytest.mark.asyncio
    async def test_load_recent_turns_with_limit(self, store: Any, mock_pool: Any) -> None:
        """load_recent_turns() respects the limit parameter."""
        mock_pool.fetch.return_value = [
            {"step_name": "step1", "messages": "[]"},
        ]
        await store.load_recent_turns("wf-1", limit=5)
        call_args = mock_pool.fetch.call_args[0]
        assert 5 in call_args

    @pytest.mark.asyncio
    async def test_load_recent_turns_empty(self, store: Any, mock_pool: Any) -> None:
        """load_recent_turns() returns empty list for no results."""
        mock_pool.fetch.return_value = []
        result = await store.load_recent_turns("wf-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_load_recent_turns_not_connected_raises(self) -> None:
        """load_recent_turns() raises RuntimeError when not connected."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="not connected"):
            await store.load_recent_turns("wf-1")


class TestTranscriptStoreSchemaIncludesIdentity:
    """Tests that _SCHEMA_SQL includes new columns."""

    def test_schema_includes_trace_id(self) -> None:
        """_SCHEMA_SQL includes trace_id column."""
        from cloud_agents.storage.transcript_store import _SCHEMA_SQL

        assert "trace_id" in _SCHEMA_SQL

    def test_schema_includes_messages(self) -> None:
        """_SCHEMA_SQL includes messages column."""
        from cloud_agents.storage.transcript_store import _SCHEMA_SQL

        assert "messages" in _SCHEMA_SQL
