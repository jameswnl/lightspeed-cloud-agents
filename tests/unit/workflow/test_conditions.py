"""Unit tests for condition evaluator."""

import pytest

from agents.workflow.conditions import evaluate_condition
from agents.workflow.state import StepResult, WorkflowState


def _make_state(**step_data: dict) -> WorkflowState:
    """Create a WorkflowState with step results."""
    steps = {}
    for name, data in step_data.items():
        steps[name] = StepResult(step_name=name, **data)
    return WorkflowState(
        workflow_id="w1", workflow_name="test",
        created_at="2026-01-01", updated_at="2026-01-01",
        steps=steps,
    )


class TestEvaluateCondition:
    """Tests for evaluate_condition."""

    def test_status_equals(self) -> None:
        """Test status == completed."""
        state = _make_state(step1={"status": "completed"})
        assert evaluate_condition("steps.step1.status == completed", state) is True

    def test_status_not_equals(self) -> None:
        """Test status != failed."""
        state = _make_state(step1={"status": "completed"})
        assert evaluate_condition("steps.step1.status != failed", state) is True

    def test_approved_true(self) -> None:
        """Test approved == true."""
        state = _make_state(approve={"status": "completed", "output": {"approved": True}})
        assert evaluate_condition("steps.approve.approved == true", state) is True

    def test_approved_false(self) -> None:
        """Test approved == false."""
        state = _make_state(approve={"status": "completed", "output": {"approved": False}})
        assert evaluate_condition("steps.approve.approved == true", state) is False

    def test_output_field_truthy(self) -> None:
        """Test truthy check on output field."""
        state = _make_state(diag={"status": "completed", "output": {"issues": ["a", "b"]}})
        assert evaluate_condition("steps.diag.output.issues", state) is True

    def test_output_field_falsy(self) -> None:
        """Test falsy check on empty list."""
        state = _make_state(diag={"status": "completed", "output": {"issues": []}})
        assert evaluate_condition("steps.diag.output.issues", state) is False

    def test_output_boolean_equals(self) -> None:
        """Test output.cluster_healthy == true."""
        state = _make_state(exec={"status": "completed", "output": {"cluster_healthy": True}})
        assert evaluate_condition("steps.exec.output.cluster_healthy == true", state) is True

    def test_and_combinator(self) -> None:
        """Test 'and' combines two conditions."""
        state = _make_state(
            a={"status": "completed", "output": {"approved": True}},
            b={"status": "completed", "output": {"healthy": True}},
        )
        assert evaluate_condition(
            "steps.a.approved == true and steps.b.output.healthy == true", state
        ) is True

    def test_or_combinator(self) -> None:
        """Test 'or' combines two conditions."""
        state = _make_state(
            a={"status": "failed"},
            b={"status": "completed"},
        )
        assert evaluate_condition(
            "steps.a.status == completed or steps.b.status == completed", state
        ) is True

    def test_missing_step_returns_false(self) -> None:
        """Test that referencing a missing step returns false."""
        state = _make_state()
        assert evaluate_condition("steps.missing.status == completed", state) is False

    def test_mixed_and_or_precedence(self) -> None:
        """Test that 'or' has lower precedence than 'and' — A or B and C = A or (B and C)."""
        state = _make_state(
            a={"status": "failed"},
            b={"status": "completed"},
            c={"status": "completed"},
        )
        # A is false, B and C are both true → A or (B and C) = true
        assert evaluate_condition(
            "steps.a.status == completed or steps.b.status == completed and steps.c.status == completed",
            state,
        ) is True

    def test_unparseable_raises(self) -> None:
        """Test that an unparseable condition raises ValueError."""
        state = _make_state()
        with pytest.raises(ValueError, match="Unparseable"):
            evaluate_condition("this is not valid", state)

    def test_none_output_returns_false(self) -> None:
        """Test that a step with no output returns false for output checks."""
        state = _make_state(step1={"status": "completed"})
        assert evaluate_condition("steps.step1.output.x == true", state) is False
