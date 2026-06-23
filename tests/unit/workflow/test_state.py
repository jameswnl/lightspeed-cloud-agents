"""Unit tests for WorkflowState model."""

from agents.workflow.state import StepResult, WorkflowState


class TestStepResult:
    """Tests for StepResult."""

    def test_default_status(self) -> None:
        """Test default status is pending."""
        r = StepResult(step_name="step1")
        assert r.status == "pending"

    def test_completed_with_output(self) -> None:
        """Test completed step with output."""
        r = StepResult(
            step_name="diag",
            status="completed",
            output={"summary": "Fixed", "cluster_healthy": True},
        )
        assert r.output["cluster_healthy"] is True

    def test_json_round_trip(self) -> None:
        """Test serialization."""
        r = StepResult(step_name="s", status="failed", error="boom")
        restored = StepResult.model_validate_json(r.model_dump_json())
        assert restored.error == "boom"


class TestWorkflowState:
    """Tests for WorkflowState."""

    def test_default_status(self) -> None:
        """Test default workflow status."""
        s = WorkflowState(
            workflow_id="w1", workflow_name="test",
            created_at="2026-01-01", updated_at="2026-01-01",
        )
        assert s.status == "running"
        assert s.steps == {}

    def test_with_steps(self) -> None:
        """Test workflow with step results."""
        s = WorkflowState(
            workflow_id="w1", workflow_name="test",
            created_at="2026-01-01", updated_at="2026-01-01",
            current_step="step2",
            steps={"step1": StepResult(step_name="step1", status="completed")},
        )
        assert s.current_step == "step2"
        assert s.steps["step1"].status == "completed"
