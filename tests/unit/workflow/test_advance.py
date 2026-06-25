"""Unit tests for advance_workflow() (Phase 8 Task 3)."""

import pytest
from unittest.mock import AsyncMock, patch

from agents.workflow.advancement import StaleStateError, advance_workflow
from agents.workflow.persistence import InMemoryPersistence
from agents.workflow.state import StepResult, WorkflowState
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec


def _two_step_definition() -> WorkflowDefinition:
    """Create a 2-step workflow definition."""
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "test-wf"},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(
                name="step1", type="agent", agent="diag",
                prompt="diagnose", output_key="result1", spawn="ephemeral",
            ),
            WorkflowStepSpec(
                name="step2", type="agent", agent="diag",
                prompt="fix {{ steps.result1.output.summary }}",
                output_key="result2", spawn="ephemeral",
            ),
        ]),
    )


def _approval_definition() -> WorkflowDefinition:
    """Create a workflow with an approval step."""
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "test-wf"},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(
                name="step1", type="agent", agent="diag",
                prompt="diagnose", output_key="result1", spawn="ephemeral",
            ),
            WorkflowStepSpec(
                name="approve", type="human-approval",
                message="OK?", output_key="approval",
            ),
        ]),
    )


def _make_state_after_step1(defn: WorkflowDefinition) -> WorkflowState:
    """Create state where step1 just completed."""
    return WorkflowState(
        workflow_id="wf-1", workflow_name="test-wf", status="running",
        definition_snapshot=defn.model_dump(mode="json"),
        steps={
            "result1": StepResult(
                step_name="step1", status="completed",
                output={"summary": "found issue", "attempt": 1},
                started_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:01:00Z",
            ),
        },
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )


class TestAdvanceWorkflow:
    """Tests for advance_workflow()."""

    @pytest.mark.asyncio
    async def test_advance_dispatches_next_step(self) -> None:
        """After step1 completes, advance dispatches step2."""
        defn = _two_step_definition()
        persistence = InMemoryPersistence()
        state = _make_state_after_step1(defn)
        await persistence.save(state)

        dispatcher = AsyncMock()
        dispatched_result = StepResult(
            step_name="step2", status="dispatched",
            output={"attempt": 1, "spawned_name": "diag-abc"},
            started_at="2026-01-01T00:02:00Z",
        )
        dispatcher.dispatch_async = AsyncMock(return_value=dispatched_result)

        await advance_workflow(persistence, dispatcher, "wf-1")

        dispatcher.dispatch_async.assert_called_once()
        call_args = dispatcher.dispatch_async.call_args
        assert call_args[0][0].name == "step2"

    @pytest.mark.asyncio
    async def test_advance_completes_workflow_after_last_step(self) -> None:
        """When the last step completes, workflow status becomes completed."""
        defn = _two_step_definition()
        persistence = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test-wf", status="running",
            definition_snapshot=defn.model_dump(mode="json"),
            steps={
                "result1": StepResult(
                    step_name="step1", status="completed",
                    output={"summary": "ok", "attempt": 1},
                    started_at="2026-01-01T00:00:00Z",
                    completed_at="2026-01-01T00:01:00Z",
                ),
                "result2": StepResult(
                    step_name="step2", status="completed",
                    output={"fix": "done", "attempt": 1},
                    started_at="2026-01-01T00:02:00Z",
                    completed_at="2026-01-01T00:03:00Z",
                ),
            },
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:03:00Z",
        )
        await persistence.save(state)

        await advance_workflow(persistence, None, "wf-1")

        updated = await persistence.load("wf-1")
        assert updated.status == "completed"

    @pytest.mark.asyncio
    async def test_advance_sets_approval_status(self) -> None:
        """When next step is human-approval, sets awaiting_approval."""
        defn = _approval_definition()
        persistence = InMemoryPersistence()
        state = _make_state_after_step1(defn)
        state.definition_snapshot = defn.model_dump(mode="json")
        await persistence.save(state)

        await advance_workflow(persistence, None, "wf-1")

        updated = await persistence.load("wf-1")
        assert updated.steps["approval"].status == "awaiting_approval"
        assert updated.status == "paused"

    @pytest.mark.asyncio
    async def test_advance_idempotent_on_retry(self) -> None:
        """Second call for same state detects step already advanced."""
        defn = _two_step_definition()
        persistence = InMemoryPersistence()
        state = _make_state_after_step1(defn)
        state.steps["result2"] = StepResult(
            step_name="step2", status="dispatched",
            output={"attempt": 1},
            started_at="2026-01-01T00:02:00Z",
        )
        await persistence.save(state)

        dispatcher = AsyncMock()
        await advance_workflow(persistence, dispatcher, "wf-1")

        dispatcher.dispatch_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_advance_unknown_workflow_is_noop(self) -> None:
        """Advancing a nonexistent workflow is a no-op."""
        persistence = InMemoryPersistence()
        await advance_workflow(persistence, None, "nonexistent")

    @pytest.mark.asyncio
    async def test_advance_failed_step_marks_workflow_failed(self) -> None:
        """When a step fails and retries exhausted, workflow fails."""
        defn = _two_step_definition()
        persistence = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test-wf", status="running",
            definition_snapshot=defn.model_dump(mode="json"),
            steps={
                "result1": StepResult(
                    step_name="step1", status="failed",
                    error="LLM timeout",
                    output={"attempt": 1},
                    started_at="2026-01-01T00:00:00Z",
                    completed_at="2026-01-01T00:01:00Z",
                ),
            },
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        await persistence.save(state)

        await advance_workflow(persistence, None, "wf-1")

        updated = await persistence.load("wf-1")
        assert updated.status == "failed"
