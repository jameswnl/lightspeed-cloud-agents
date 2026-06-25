"""Unit tests for step result ingestion (Phase 8 Task 1)."""

import pytest

from agents.workflow.advancement import IngestError, ingest_step_result
from agents.workflow.persistence import InMemoryPersistence
from agents.workflow.state import StepResult, StepResultPayload, WorkflowState


def _make_state(
    step_name: str = "diagnose",
    step_status: str = "dispatched",
    attempt: int = 1,
) -> WorkflowState:
    """Create a workflow state with one dispatched step."""
    return WorkflowState(
        workflow_id="wf-1",
        workflow_name="test",
        status="running",
        steps={
            step_name: StepResult(
                step_name=step_name,
                status=step_status,
                started_at="2026-01-01T00:00:00Z",
                output={"spawned_name": "agent-abc", "attempt": attempt},
            ),
        },
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _make_payload(
    status: str = "completed",
    attempt: int = 1,
    output: dict | None = None,
) -> StepResultPayload:
    """Create a step result payload."""
    return StepResultPayload(
        status=status,
        output=output or {"summary": "done"},
        completed_at="2026-01-01T00:01:00Z",
        attempt=attempt,
    )


class TestIngestStepResult:
    """Tests for ingest_step_result()."""

    @pytest.mark.asyncio
    async def test_ingest_valid_result_updates_step(self) -> None:
        """Happy path: dispatched step receives completed result."""
        persistence = InMemoryPersistence()
        state = _make_state()
        await persistence.save(state)

        payload = _make_payload()
        updated = await ingest_step_result(persistence, "wf-1", "diagnose", payload)

        assert updated.steps["diagnose"].status == "completed"
        assert updated.steps["diagnose"].output["summary"] == "done"
        assert updated.steps["diagnose"].completed_at == "2026-01-01T00:01:00Z"

    @pytest.mark.asyncio
    async def test_ingest_unknown_workflow_raises_404(self) -> None:
        """Unknown workflow ID raises IngestError with 404."""
        persistence = InMemoryPersistence()
        payload = _make_payload()

        with pytest.raises(IngestError) as exc_info:
            await ingest_step_result(persistence, "nonexistent", "diagnose", payload)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_ingest_unknown_step_raises_404(self) -> None:
        """Unknown step name raises IngestError with 404."""
        persistence = InMemoryPersistence()
        state = _make_state()
        await persistence.save(state)

        payload = _make_payload()
        with pytest.raises(IngestError) as exc_info:
            await ingest_step_result(persistence, "wf-1", "nonexistent", payload)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_ingest_step_already_completed_raises_409(self) -> None:
        """Step already terminal rejects with 409."""
        persistence = InMemoryPersistence()
        state = _make_state(step_status="completed")
        await persistence.save(state)

        payload = _make_payload(attempt=2)
        with pytest.raises(IngestError) as exc_info:
            await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_ingest_duplicate_same_attempt_idempotent(self) -> None:
        """Duplicate callback for same attempt + same status is idempotent."""
        persistence = InMemoryPersistence()
        state = _make_state(step_status="completed")
        state.steps["diagnose"].output = {"attempt": 1}
        await persistence.save(state)

        payload = _make_payload(attempt=1, status="completed")
        result = await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert result.steps["diagnose"].status == "completed"

    @pytest.mark.asyncio
    async def test_ingest_stale_attempt_rejected(self) -> None:
        """Callback from a prior attempt is rejected with 409."""
        persistence = InMemoryPersistence()
        state = _make_state(attempt=2)
        await persistence.save(state)

        payload = _make_payload(attempt=1)
        with pytest.raises(IngestError) as exc_info:
            await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_ingest_step_not_dispatched_raises_409(self) -> None:
        """Step in 'running' status rejects ingestion."""
        persistence = InMemoryPersistence()
        state = _make_state(step_status="running")
        await persistence.save(state)

        payload = _make_payload()
        with pytest.raises(IngestError) as exc_info:
            await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_ingest_pending_step_accepted(self) -> None:
        """Step in 'pending' status accepts ingestion (edge case)."""
        persistence = InMemoryPersistence()
        state = _make_state(step_status="pending")
        await persistence.save(state)

        payload = _make_payload()
        updated = await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert updated.steps["diagnose"].status == "completed"

    @pytest.mark.asyncio
    async def test_ingest_failed_result(self) -> None:
        """Failed step result is ingested correctly."""
        persistence = InMemoryPersistence()
        state = _make_state()
        await persistence.save(state)

        payload = StepResultPayload(
            status="failed",
            error="LLM timeout",
            completed_at="2026-01-01T00:01:00Z",
            attempt=1,
        )
        updated = await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert updated.steps["diagnose"].status == "failed"
        assert updated.steps["diagnose"].error == "LLM timeout"
        assert updated.status == "failed"

    @pytest.mark.asyncio
    async def test_ingest_increments_version(self) -> None:
        """Ingestion increments the workflow state version."""
        persistence = InMemoryPersistence()
        state = _make_state()
        await persistence.save(state)
        original_version = state.version

        payload = _make_payload()
        updated = await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert updated.version == original_version + 1

    @pytest.mark.asyncio
    async def test_ingest_preserves_existing_output_fields(self) -> None:
        """Ingestion merges payload output into existing step output."""
        persistence = InMemoryPersistence()
        state = _make_state()
        state.steps["diagnose"].output = {
            "spawned_name": "agent-abc",
            "run_id": "run-123",
            "attempt": 1,
        }
        await persistence.save(state)

        payload = _make_payload(output={"summary": "done"})
        updated = await ingest_step_result(persistence, "wf-1", "diagnose", payload)
        assert updated.steps["diagnose"].output["spawned_name"] == "agent-abc"
        assert updated.steps["diagnose"].output["summary"] == "done"
