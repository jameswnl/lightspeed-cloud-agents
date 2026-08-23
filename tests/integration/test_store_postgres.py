"""Integration tests: RunStateStore and TranscriptStore with real PostgreSQL.

These tests use the PostgreSQL service available in CI via the
integration_tests.yaml workflow. They verify that:
- Schema creation works (CREATE TABLE IF NOT EXISTS)
- Identity columns are present and queryable
- Alembic migrations apply cleanly
- Store methods work end-to-end with real SQL

Requires: PostgreSQL on localhost:5432 (user: temporal, password: temporal, db: temporal)
Set RUN_STATE_DB_URL to override.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

_DEFAULT_DB_URL = "postgresql://temporal:temporal@localhost:5432/temporal"
_DB_URL = os.environ.get("RUN_STATE_DB_URL", _DEFAULT_DB_URL)


def _is_postgres_available() -> bool:
    """Check if PostgreSQL is reachable."""
    try:
        import asyncio

        import asyncpg

        async def check() -> bool:
            try:
                conn = await asyncpg.connect(_DEFAULT_DB_URL)
                await conn.close()
                return True
            except Exception:
                return False

        return asyncio.run(check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_STATE_DB_URL") and not _is_postgres_available(),
    reason="PostgreSQL not available (set RUN_STATE_DB_URL or run in CI)",
)


class TestRunStateStorePostgres:
    """RunStateStore with real PostgreSQL."""

    @pytest.fixture
    async def store(self):
        """Create a connected RunStateStore."""
        from cloud_agents.storage.run_state_store import RunStateStore

        s = RunStateStore(_DB_URL)
        await s.connect()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_create_and_get_with_identity(self, store) -> None:
        """Create a workflow with identity fields and retrieve them."""
        wf_id = f"wf-test-{uuid.uuid4().hex[:8]}"
        try:
            await store.create(
                workflow_id=wf_id,
                workflow_name="test-identity",
                definition={"apiVersion": "v1"},
                provider={"name": "openai"},
                authz_context={},
                user_id="jwong",
                session_id="ses-abc",
            )

            row = await store.get(wf_id)
            assert row is not None
            assert row["workflow_id"] == wf_id
            assert row["user_id"] == "jwong"
            assert row["session_id"] == "ses-abc"
            assert row["parent_workflow_id"] is None
        finally:
            await store.delete(wf_id)

    @pytest.mark.asyncio
    async def test_create_with_parent_workflow(self, store) -> None:
        """Create a child workflow linked to a parent."""
        parent_id = f"wf-parent-{uuid.uuid4().hex[:8]}"
        child_id = f"wf-child-{uuid.uuid4().hex[:8]}"
        try:
            await store.create(
                workflow_id=parent_id,
                workflow_name="parent",
                definition={},
                provider={},
                authz_context={},
                user_id="user-a",
            )
            await store.create(
                workflow_id=child_id,
                workflow_name="child",
                definition={},
                provider={},
                authz_context={},
                user_id="user-b",
                parent_workflow_id=parent_id,
            )

            child = await store.get(child_id)
            assert child["parent_workflow_id"] == parent_id
        finally:
            await store.delete(child_id)
            await store.delete(parent_id)

    @pytest.mark.asyncio
    async def test_list_by_user(self, store) -> None:
        """list_by_user returns only workflows for that user."""
        user = f"user-{uuid.uuid4().hex[:8]}"
        wf1 = f"wf-{uuid.uuid4().hex[:8]}"
        wf2 = f"wf-{uuid.uuid4().hex[:8]}"
        wf_other = f"wf-{uuid.uuid4().hex[:8]}"
        try:
            await store.create(wf1, "w1", {}, {}, {}, user_id=user)
            await store.create(wf2, "w2", {}, {}, {}, user_id=user)
            await store.create(wf_other, "w3", {}, {}, {}, user_id="other-user")

            results = await store.list_by_user(user)
            result_ids = [r["workflow_id"] for r in results]
            assert wf1 in result_ids
            assert wf2 in result_ids
            assert wf_other not in result_ids
        finally:
            await store.delete(wf1)
            await store.delete(wf2)
            await store.delete(wf_other)

    @pytest.mark.asyncio
    async def test_list_by_session(self, store) -> None:
        """list_by_session returns workflows grouped by session."""
        session = f"ses-{uuid.uuid4().hex[:8]}"
        wf1 = f"wf-{uuid.uuid4().hex[:8]}"
        wf2 = f"wf-{uuid.uuid4().hex[:8]}"
        try:
            await store.create(wf1, "w1", {}, {}, {}, session_id=session)
            await store.create(wf2, "w2", {}, {}, {}, session_id=session)

            results = await store.list_by_session(session)
            result_ids = [r["workflow_id"] for r in results]
            assert wf1 in result_ids
            assert wf2 in result_ids
        finally:
            await store.delete(wf1)
            await store.delete(wf2)


class TestTranscriptStorePostgres:
    """TranscriptStore with real PostgreSQL."""

    @pytest.fixture
    async def store(self):
        """Create a connected TranscriptStore."""
        from cloud_agents.storage.transcript_store import TranscriptStore

        s = TranscriptStore(_DB_URL)
        await s.connect()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_save_and_get_with_trace_id(self, store) -> None:
        """Save a transcript with trace_id and retrieve it."""
        from cloud_agents.workflow.core.models import StepTranscript, TranscriptEvent

        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        transcript = StepTranscript(
            step_name="turn-1",
            events=[TranscriptEvent(ts=0.0, type="llm.call", data={"model": "gpt-4o"})],
            input_tokens=50,
            output_tokens=20,
            duration_ms=500,
        )
        try:
            await store.save(
                workflow_id=wf_id,
                step_name="turn-1",
                transcript=transcript,
                trace_id="tr-abc123",
            )

            result = await store.get(wf_id, "turn-1")
            assert result is not None
            assert result["trace_id"] == "tr-abc123"
        finally:
            await store.delete_workflow(wf_id)

    @pytest.mark.asyncio
    async def test_save_and_get_with_messages(self, store) -> None:
        """Save conversation messages and retrieve them."""
        from cloud_agents.workflow.core.models import StepTranscript
        from cloud_agents.workflow.executor.step.conversation import (
            ConversationMessage,
        )

        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        messages = [
            ConversationMessage(role="user", content="What pods?").to_dict(),
            ConversationMessage(role="assistant", content="8 pods.").to_dict(),
        ]
        transcript = StepTranscript(
            step_name="turn-1",
            events=[],
            input_tokens=30,
            output_tokens=10,
        )
        try:
            await store.save(
                workflow_id=wf_id,
                step_name="turn-1",
                transcript=transcript,
                messages=messages,
            )

            result = await store.get(wf_id, "turn-1")
            assert result is not None
            assert result["messages"] is not None
            assert len(result["messages"]) == 2
            assert result["messages"][0]["role"] == "user"
        finally:
            await store.delete_workflow(wf_id)

    @pytest.mark.asyncio
    async def test_load_recent_turns(self, store) -> None:
        """load_recent_turns returns turns in order with limit."""
        from cloud_agents.workflow.core.models import StepTranscript

        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        try:
            for i in range(5):
                transcript = StepTranscript(
                    step_name=f"turn-{i}",
                    events=[],
                    input_tokens=10,
                    output_tokens=5,
                )
                await store.save(
                    workflow_id=wf_id,
                    step_name=f"turn-{i}",
                    transcript=transcript,
                    messages=[{"role": "user", "content": f"msg-{i}"}],
                )

            turns = await store.load_recent_turns(wf_id, limit=3)
            assert len(turns) == 3
            # Most recent turns
            step_names = [t["step_name"] for t in turns]
            assert "turn-4" in step_names
            assert "turn-3" in step_names
            assert "turn-2" in step_names
        finally:
            await store.delete_workflow(wf_id)


class TestAlembicMigrations:
    """Test Alembic migrations against real PostgreSQL."""

    @pytest.mark.asyncio
    async def test_alembic_upgrade_head(self) -> None:
        """Alembic migrations apply cleanly to a real database."""
        import subprocess

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env={**os.environ, "RUN_STATE_DB_URL": _DB_URL},
            timeout=30,
        )
        assert result.returncode == 0, f"Alembic upgrade failed: {result.stderr}"

    @pytest.mark.asyncio
    async def test_alembic_downgrade_base(self) -> None:
        """Alembic migrations can be rolled back."""
        import subprocess

        # First upgrade
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env={**os.environ, "RUN_STATE_DB_URL": _DB_URL},
            timeout=30,
        )

        # Then downgrade
        result = subprocess.run(
            ["uv", "run", "alembic", "downgrade", "base"],
            capture_output=True,
            text=True,
            env={**os.environ, "RUN_STATE_DB_URL": _DB_URL},
            timeout=30,
        )
        assert result.returncode == 0, f"Alembic downgrade failed: {result.stderr}"

        # Re-upgrade for clean state
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env={**os.environ, "RUN_STATE_DB_URL": _DB_URL},
            timeout=30,
        )
