"""Tests for the WorkflowRunner interface and data models."""

import pytest

from cloud_agents.workflow.executor.base import (
    ApprovalDecision,
    WorkflowRunner,
    WorkflowStatus,
)


class TestWorkflowStatus:
    """Tests for WorkflowStatus dataclass."""

    def test_defaults(self) -> None:
        """Empty status has sensible defaults."""
        status = WorkflowStatus(workflow_id="wf-1", status="running")
        assert status.workflow_id == "wf-1"
        assert status.status == "running"
        assert status.steps == {}
        assert status.events == []
        assert status.is_terminal is False

    def test_terminal_state(self) -> None:
        """Terminal status is represented correctly."""
        status = WorkflowStatus(
            workflow_id="wf-1",
            status="completed",
            steps={"diag": {"status": "completed", "output": {"ok": True}}},
            events=[{"type": "workflow.completed"}],
            is_terminal=True,
        )
        assert status.is_terminal is True
        assert "diag" in status.steps


class TestApprovalDecision:
    """Tests for ApprovalDecision dataclass."""

    def test_minimal(self) -> None:
        """Approval with required fields only."""
        decision = ApprovalDecision(step_name="approve-fix", decision="approved")
        assert decision.step_name == "approve-fix"
        assert decision.decision == "approved"
        assert decision.approver == ""
        assert decision.selected_option_id is None

    def test_full(self) -> None:
        """Approval with all fields."""
        decision = ApprovalDecision(
            step_name="approve-fix",
            decision="approved",
            approver="user:jwong",
            selected_option_id="option-1",
        )
        assert decision.approver == "user:jwong"
        assert decision.selected_option_id == "option-1"


class TestWorkflowRunnerIsAbstract:
    """Verify WorkflowRunner cannot be instantiated directly."""

    def test_cannot_instantiate(self) -> None:
        """ABC prevents direct instantiation."""
        with pytest.raises(TypeError):
            WorkflowRunner()  # type: ignore[abstract]

    def test_requires_all_methods(self) -> None:
        """Subclass missing any method cannot be instantiated."""

        class PartialRunner(WorkflowRunner):
            async def start(self, input):
                return "wf-1"

        with pytest.raises(TypeError):
            PartialRunner()  # type: ignore[abstract]
