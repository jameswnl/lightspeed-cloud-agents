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

    @pytest.mark.asyncio
    async def test_recovery_polls_agent_and_recovers_completed(self) -> None:
        """Poller polls agent pod and recovers completed result."""
        from unittest.mock import AsyncMock, MagicMock
        from agents.models import AgentRunResponse, RunState, RunStatus

        p = InMemoryPersistence()
        state = _make_state()
        state.definition_snapshot = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "agent": "diag",
                 "prompt": "test", "output_key": "r1", "spawn": "ephemeral"},
            ]},
        }
        state.steps["r1"] = StepResult(
            step_name="s1", status="dispatched",
            started_at="2020-01-01T00:00:00Z",
            output={
                "spawned_name": "diag-abc",
                "run_id": "run-123",
                "endpoint": "http://diag-abc:8080",
                "attempt": 1,
            },
        )
        await p.save(state)

        mock_run_state = RunState(
            run_id="run-123",
            status=RunStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            result=AgentRunResponse(
                output={"summary": "fixed"},
                output_type="str",
                usage={"input_tokens": 1, "output_tokens": 1},
                agent_name="diag",
                success=True,
            ),
        )

        mock_client = AsyncMock()
        mock_client.poll_run = AsyncMock(return_value=mock_run_state)

        spawner = AsyncMock()
        spawner.destroy = AsyncMock()

        poller = RecoveryPoller(
            p, step_timeout=10, spawner=spawner,
            client_factory=lambda endpoint: mock_client,
        )
        await poller._poll_once()

        updated = await p.load("wf-1")
        assert updated.steps["r1"].status == "completed"
        assert updated.steps["r1"].output["summary"] == "fixed"
        spawner.destroy.assert_called_once_with("diag-abc")

    @pytest.mark.asyncio
    async def test_recovery_marks_failed_when_pod_unreachable(self) -> None:
        """Poller marks step failed when pod is unreachable."""
        from unittest.mock import AsyncMock

        p = InMemoryPersistence()
        state = _make_state()
        state.definition_snapshot = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "agent": "diag",
                 "prompt": "test", "output_key": "r1", "spawn": "ephemeral"},
            ]},
        }
        state.steps["r1"] = StepResult(
            step_name="s1", status="dispatched",
            started_at="2020-01-01T00:00:00Z",
            output={
                "spawned_name": "diag-abc",
                "run_id": "run-123",
                "endpoint": "http://diag-abc:8080",
                "attempt": 1,
            },
        )
        await p.save(state)

        mock_client = AsyncMock()
        mock_client.poll_run = AsyncMock(side_effect=Exception("Connection refused"))

        spawner = AsyncMock()

        poller = RecoveryPoller(
            p, step_timeout=10, spawner=spawner,
            client_factory=lambda endpoint: mock_client,
        )
        await poller._poll_once()

        updated = await p.load("wf-1")
        assert updated.steps["r1"].status == "failed"

    @pytest.mark.asyncio
    async def test_recovery_no_run_id_pod_unreachable_marks_failed(self) -> None:
        """Poller marks step failed when run_id is None and pod unreachable."""
        from unittest.mock import AsyncMock

        p = InMemoryPersistence()
        state = _make_state()
        state.definition_snapshot = {
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "agent": "diag",
                 "prompt": "test", "output_key": "r1", "spawn": "ephemeral"},
            ]},
        }
        state.steps["r1"] = StepResult(
            step_name="s1", status="dispatched",
            started_at="2020-01-01T00:00:00Z",
            output={
                "spawned_name": "diag-abc",
                "run_id": None,
                "endpoint": "http://diag-abc:8080",
                "attempt": 1,
            },
        )
        await p.save(state)

        mock_client = AsyncMock()
        mock_client.healthz = AsyncMock(return_value=False)

        spawner = AsyncMock()

        poller = RecoveryPoller(
            p, step_timeout=10, spawner=spawner,
            client_factory=lambda endpoint: mock_client,
        )
        await poller._poll_once()

        updated = await p.load("wf-1")
        assert updated.steps["r1"].status == "failed"
        assert "pod never spawned" in updated.steps["r1"].error.lower() or "dispatch interrupted" in updated.steps["r1"].error.lower()
