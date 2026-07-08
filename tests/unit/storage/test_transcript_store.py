"""Unit tests for PostgreSQL transcript store (TDD)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_agents.workflow.temporal_models import StepTranscript, TranscriptEvent


class TestTranscriptStoreInit:
    """Tests for TranscriptStore initialization and configuration."""

    def test_import(self) -> None:
        """TranscriptStore can be imported from storage package."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        assert TranscriptStore is not None

    def test_constructor_accepts_db_url(self) -> None:
        """TranscriptStore accepts a database URL."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://user:pass@localhost/testdb")
        assert store._db_url == "postgresql://user:pass@localhost/testdb"

    def test_constructor_accepts_retention_days(self) -> None:
        """TranscriptStore accepts a retention_days parameter."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(
            db_url="postgresql://localhost/testdb",
            retention_days=14,
        )
        assert store._retention_days == 14

    def test_default_retention_days(self) -> None:
        """Default retention_days is 30."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        assert store._retention_days == 30

    def test_pool_not_created_until_connect(self) -> None:
        """Connection pool is None before connect() is called."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        assert store._pool is None


class TestTranscriptStoreConnect:
    """Tests for TranscriptStore.connect() lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_creates_pool(self) -> None:
        """connect() creates an asyncpg connection pool."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")

        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)

        with patch(
            "cloud_agents.storage.transcript_store.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ) as mock_create:
            await store.connect()

            mock_create.assert_called_once_with("postgresql://localhost/testdb")
            assert store._pool is mock_pool

    @pytest.mark.asyncio
    async def test_connect_runs_schema_migration(self) -> None:
        """connect() executes CREATE TABLE IF NOT EXISTS."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")

        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)

        with patch(
            "cloud_agents.storage.transcript_store.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            await store.connect()

            # Should have called execute at least once for schema
            assert mock_pool.execute.call_count >= 1
            first_call = mock_pool.execute.call_args_list[0]
            sql = first_call[0][0]
            assert "CREATE TABLE IF NOT EXISTS" in sql
            assert "step_transcripts" in sql

    @pytest.mark.asyncio
    async def test_close_closes_pool(self) -> None:
        """close() closes the connection pool."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")

        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)

        with patch(
            "cloud_agents.storage.transcript_store.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ):
            await store.connect()
            await store.close()

            mock_pool.close.assert_called_once()


class TestTranscriptStoreSave:
    """Tests for TranscriptStore.save()."""

    @pytest.fixture
    def sample_transcript(self) -> StepTranscript:
        """Create a sample transcript for testing."""
        return StepTranscript(
            step_name="diagnose",
            events=[
                TranscriptEvent(
                    ts="2026-01-01T00:00:00Z",
                    type="tool_call",
                    data={"name": "kubectl_get", "input": "get pods"},
                ),
                TranscriptEvent(
                    ts="2026-01-01T00:00:01Z",
                    type="tool_result",
                    data={"name": "kubectl_get", "output": "pod-1 Running"},
                ),
            ],
            cost_usd=0.05,
            input_tokens=100,
            output_tokens=50,
            duration_ms=5000,
        )

    @pytest.mark.asyncio
    async def test_save_executes_upsert(self, sample_transcript: StepTranscript) -> None:
        """save() executes an INSERT ... ON CONFLICT UPDATE."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        await store.save("wf-123", "diagnose", sample_transcript)

        # Find the upsert call (not the schema migration)
        calls = mock_pool.execute.call_args_list
        upsert_calls = [c for c in calls if "INSERT INTO step_transcripts" in str(c)]
        assert len(upsert_calls) == 1

        sql = upsert_calls[0][0][0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql

    @pytest.mark.asyncio
    async def test_save_passes_correct_parameters(
        self, sample_transcript: StepTranscript
    ) -> None:
        """save() passes workflow_id, step_name, and transcript data."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        await store.save("wf-123", "diagnose", sample_transcript)

        calls = mock_pool.execute.call_args_list
        upsert_calls = [c for c in calls if "INSERT INTO step_transcripts" in str(c)]
        args = upsert_calls[0][0]
        # Should pass workflow_id, step_name, events JSON, cost, tokens, duration
        assert args[1] == "wf-123"
        assert args[2] == "diagnose"

    @pytest.mark.asyncio
    async def test_save_without_pool_raises(self, sample_transcript: StepTranscript) -> None:
        """save() raises RuntimeError when not connected."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")

        with pytest.raises(RuntimeError, match="not connected"):
            await store.save("wf-123", "diagnose", sample_transcript)


class TestTranscriptStoreGet:
    """Tests for TranscriptStore.get()."""

    @pytest.mark.asyncio
    async def test_get_returns_transcript(self) -> None:
        """get() returns a StepTranscript from database row."""
        import json

        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        # Mock fetchrow to return a database row
        events_json = json.dumps([
            {"ts": "2026-01-01T00:00:00Z", "type": "tool_call", "data": {"name": "kubectl"}},
        ])
        mock_row = {
            "events": events_json,
            "cost_usd": 0.05,
            "input_tokens": 100,
            "output_tokens": 50,
            "duration_ms": 5000,
        }
        mock_pool.fetchrow = AsyncMock(return_value=mock_row)

        result = await store.get("wf-123", "diagnose")

        assert result is not None
        assert result.step_name == "diagnose"
        assert len(result.events) == 1
        assert result.events[0].type == "tool_call"
        assert result.cost_usd == 0.05
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.duration_ms == 5000

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(self) -> None:
        """get() returns None when no row matches."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool
        mock_pool.fetchrow = AsyncMock(return_value=None)

        result = await store.get("wf-nonexistent", "step1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_without_pool_raises(self) -> None:
        """get() raises RuntimeError when not connected."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")

        with pytest.raises(RuntimeError, match="not connected"):
            await store.get("wf-123", "diagnose")


class TestTranscriptStoreListSteps:
    """Tests for TranscriptStore.list_steps()."""

    @pytest.mark.asyncio
    async def test_list_steps_returns_step_names(self) -> None:
        """list_steps() returns list of step names for a workflow."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        mock_rows = [
            {"step_name": "diagnose"},
            {"step_name": "fix"},
        ]
        mock_pool.fetch = AsyncMock(return_value=mock_rows)

        result = await store.list_steps("wf-123")

        assert result == ["diagnose", "fix"]

    @pytest.mark.asyncio
    async def test_list_steps_empty_when_no_workflow(self) -> None:
        """list_steps() returns empty list for unknown workflow."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool
        mock_pool.fetch = AsyncMock(return_value=[])

        result = await store.list_steps("wf-nonexistent")

        assert result == []


class TestTranscriptStoreDeleteWorkflow:
    """Tests for TranscriptStore.delete_workflow()."""

    @pytest.mark.asyncio
    async def test_delete_workflow_executes_delete(self) -> None:
        """delete_workflow() executes DELETE for the workflow."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(db_url="postgresql://localhost/testdb")
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        await store.delete_workflow("wf-123")

        calls = mock_pool.execute.call_args_list
        delete_calls = [c for c in calls if "DELETE FROM" in str(c)]
        assert len(delete_calls) == 1
        sql = delete_calls[0][0][0]
        assert "step_transcripts" in sql
        assert delete_calls[0][0][1] == "wf-123"


class TestTranscriptStoreCleanupExpired:
    """Tests for TranscriptStore.cleanup_expired()."""

    @pytest.mark.asyncio
    async def test_cleanup_expired_deletes_old_rows(self) -> None:
        """cleanup_expired() deletes rows older than retention_days."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(
            db_url="postgresql://localhost/testdb",
            retention_days=7,
        )
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="DELETE 5")
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        deleted = await store.cleanup_expired()

        calls = mock_pool.execute.call_args_list
        cleanup_calls = [c for c in calls if "created_at" in str(c)]
        assert len(cleanup_calls) == 1
        sql = cleanup_calls[0][0][0]
        assert "DELETE FROM step_transcripts" in sql
        assert "created_at" in sql
        assert "interval" in sql.lower() or "$1" in sql
        # Verify retention_days=7 is passed as the parameter
        retention_arg = cleanup_calls[0][0][1]
        assert retention_arg == "7"

    @pytest.mark.asyncio
    async def test_cleanup_expired_returns_count(self) -> None:
        """cleanup_expired() returns the number of deleted rows."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore(
            db_url="postgresql://localhost/testdb",
            retention_days=7,
        )
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="DELETE 5")
        mock_pool.fetchval = AsyncMock(return_value=None)
        store._pool = mock_pool

        deleted = await store.cleanup_expired()

        assert deleted == 5


class TestTranscriptStoreFromEnv:
    """Tests for TranscriptStore.from_env() factory method."""

    def test_from_env_reads_transcript_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() reads TRANSCRIPT_DB_URL."""
        monkeypatch.setenv("TRANSCRIPT_DB_URL", "postgresql://custom:pass@db:5432/transcripts")

        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore.from_env()
        assert store._db_url == "postgresql://custom:pass@db:5432/transcripts"

    def test_from_env_returns_none_when_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() returns None when TRANSCRIPT_DB_URL is not set."""
        monkeypatch.delenv("TRANSCRIPT_DB_URL", raising=False)

        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore.from_env()
        assert store is None

    def test_from_env_reads_retention_days(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() reads TRANSCRIPT_RETENTION_DAYS."""
        monkeypatch.setenv("TRANSCRIPT_DB_URL", "postgresql://localhost/testdb")
        monkeypatch.setenv("TRANSCRIPT_RETENTION_DAYS", "14")

        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore.from_env()
        assert store is not None
        assert store._retention_days == 14

    def test_from_env_default_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() defaults to 30 retention days."""
        monkeypatch.setenv("TRANSCRIPT_DB_URL", "postgresql://localhost/testdb")
        monkeypatch.delenv("TRANSCRIPT_RETENTION_DAYS", raising=False)

        from cloud_agents.storage.transcript_store import TranscriptStore

        store = TranscriptStore.from_env()
        assert store is not None
        assert store._retention_days == 30
