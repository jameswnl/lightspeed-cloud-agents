"""Unit tests for WorkflowExecutor."""

import pytest
from unittest.mock import AsyncMock

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.executor import WorkflowExecutor


def _make_definition(steps: list[dict]) -> WorkflowDefinition:
    """Create a WorkflowDefinition from step dicts."""
    step_specs = [WorkflowStepSpec(**s) for s in steps]
    return WorkflowDefinition(
        apiVersion="v1",
        kind="AgentWorkflow",
        metadata={"name": "test-workflow"},
        spec=WorkflowSpec(steps=step_specs),
    )


def _make_agent_response(output: dict, success: bool = True) -> AgentRunResponse:
    """Create a mock AgentRunResponse."""
    return AgentRunResponse(
        output=output,
        output_type="DiagnosticReport",
        usage={"input_tokens": 10, "output_tokens": 20},
        agent_name="test-agent",
        success=success,
    )


def _mock_registry() -> AgentRegistry:
    """Create a registry with a diagnostic agent."""
    return AgentRegistry({"diagnostic-agent": "http://diag:8080"})


class TestWorkflowExecutorBasic:
    """Basic executor tests."""

    @pytest.mark.asyncio
    async def test_single_agent_step(self) -> None:
        """Test executing a single agent step."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Do something", "output_key": "result"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_agent_response(
            {"summary": "Done", "cluster_healthy": True}
        ))

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "completed"
        assert state.steps["result"].status == "completed"
        assert state.steps["result"].output["summary"] == "Done"

    @pytest.mark.asyncio
    async def test_two_step_workflow(self) -> None:
        """Test executing two sequential agent steps."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Diagnose", "output_key": "diagnosis"},
            {"name": "step2", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix based on {{ steps.diagnosis.output.summary }}",
             "output_key": "fix"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_agent_response({"summary": "web-02 is broken"}),
            _make_agent_response({"summary": "Fixed web-02"}),
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "completed"
        assert len(state.steps) == 2
        assert state.steps["diagnosis"].output["summary"] == "web-02 is broken"
        assert state.steps["fix"].output["summary"] == "Fixed web-02"
        assert client.run.call_count == 2

    @pytest.mark.asyncio
    async def test_agent_failure_fails_workflow(self) -> None:
        """Test that an agent failure fails the workflow."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Do something", "output_key": "result"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Agent down"))

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "failed"
        assert state.steps["result"].status == "failed"
        assert "Agent down" in state.steps["result"].error


class TestWorkflowExecutorConditions:
    """Tests for conditional step execution."""

    @pytest.mark.asyncio
    async def test_condition_true_executes(self) -> None:
        """Test that a true condition allows execution."""
        defn = _make_definition([
            {"name": "check", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "check"},
            {"name": "fix", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix", "output_key": "fix",
             "condition": "steps.check.output.needs_fix == true"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_agent_response({"needs_fix": True}),
            _make_agent_response({"summary": "Fixed"}),
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.steps["fix"].status == "completed"

    @pytest.mark.asyncio
    async def test_condition_false_skips(self) -> None:
        """Test that a false condition skips the step."""
        defn = _make_definition([
            {"name": "check", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "check"},
            {"name": "fix", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix", "output_key": "fix",
             "condition": "steps.check.output.needs_fix == true"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_agent_response({"needs_fix": False}),
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.steps["fix"].status == "skipped"
        assert client.run.call_count == 1


class TestWorkflowExecutorApproval:
    """Tests for human approval steps."""

    @pytest.mark.asyncio
    async def test_approval_step_pauses(self) -> None:
        """Test that an approval step pauses the workflow."""
        defn = _make_definition([
            {"name": "diagnose", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Diagnose", "output_key": "diagnosis"},
            {"name": "approve", "type": "human-approval",
             "message": "Approve?", "output_key": "approval"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_agent_response({"summary": "ok"}))

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "paused"
        assert state.current_step == "approve"
        assert state.steps["approval"].status == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_resume_after_approval(self) -> None:
        """Test resuming a workflow after approval."""
        defn = _make_definition([
            {"name": "diagnose", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Diagnose", "output_key": "diagnosis"},
            {"name": "approve", "type": "human-approval",
             "message": "Approve?", "output_key": "approval"},
            {"name": "execute", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Execute", "output_key": "execution",
             "condition": "steps.approval.approved == true"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_agent_response({"summary": "diagnosed"}),
            _make_agent_response({"summary": "executed"}),
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()
        assert state.status == "paused"

        state = await executor.resume(state.workflow_id, approved=True)
        assert state.status == "completed"
        assert state.steps["approval"].output["approved"] is True
        assert state.steps["execution"].status == "completed"

    @pytest.mark.asyncio
    async def test_approval_timeout_fails_workflow(self) -> None:
        """Test that an expired approval timeout fails the workflow."""
        defn = _make_definition([
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "approval",
             "timeout_seconds": 0},
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: AsyncMock())
        state = await executor.run()
        assert state.status == "paused"

        import asyncio
        await asyncio.sleep(0.01)

        state = await executor.get_state(state.workflow_id)
        assert state.status == "failed"
        assert "timed out" in state.steps["approval"].error

    @pytest.mark.asyncio
    async def test_rejection_stops_workflow(self) -> None:
        """Test that rejecting stops the workflow."""
        defn = _make_definition([
            {"name": "approve", "type": "human-approval",
             "message": "Approve?", "output_key": "approval"},
            {"name": "execute", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Execute", "output_key": "execution",
             "condition": "steps.approval.approved == true"},
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: AsyncMock())
        state = await executor.run()
        assert state.status == "paused"

        state = await executor.resume(state.workflow_id, approved=False)
        assert state.steps["approval"].output["approved"] is False
        assert state.steps["approval"].status == "failed"
        assert state.steps["approval"].error == "Approval rejected by human"
        assert state.status == "failed"
        assert "execution" not in state.steps
