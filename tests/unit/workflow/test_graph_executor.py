"""Unit tests for GraphExecutor — pydantic-graph based workflow executor."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.advisory import AdvisoryEnforcer
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.graph_executor import GraphExecutor


def _make_definition(steps: list[dict]) -> WorkflowDefinition:
    """Create a WorkflowDefinition from step dicts."""
    step_specs = [WorkflowStepSpec(**{**s, "spawn": s.get("spawn", "pre-deployed")}) for s in steps]
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "test-workflow"},
        spec=WorkflowSpec(steps=step_specs),
    )


def _make_response(output: dict, success: bool = True) -> AgentRunResponse:
    """Create a mock AgentRunResponse."""
    return AgentRunResponse(
        output=output, output_type="DiagnosticReport",
        usage={"input_tokens": 10, "output_tokens": 20},
        agent_name="test-agent", success=success,
    )


def _mock_registry() -> AgentRegistry:
    return AgentRegistry({"diagnostic-agent": "http://diag:8080"})


class TestGraphExecutorBasic:
    """Basic graph executor tests."""

    @pytest.mark.asyncio
    async def test_single_step(self) -> None:
        """Test single agent step execution."""
        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "r1"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"summary": "ok"}))

        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "completed"
        assert state.steps["r1"].status == "completed"
        assert state.steps["r1"].output["summary"] == "ok"

    @pytest.mark.asyncio
    async def test_two_steps(self) -> None:
        """Test two sequential agent steps."""
        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Diagnose", "output_key": "d"},
            {"name": "s2", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix", "output_key": "f"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_response({"summary": "broken"}),
            _make_response({"summary": "fixed"}),
        ])

        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "completed"
        assert len(state.steps) == 2

    @pytest.mark.asyncio
    async def test_agent_failure(self) -> None:
        """Test that agent failure fails the workflow."""
        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "r1"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Agent down"))

        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "failed"


class TestGraphExecutorApproval:
    """Approval tests for GraphExecutor."""

    @pytest.mark.asyncio
    async def test_approval_pauses(self) -> None:
        """Test that approval step pauses the workflow."""
        defn = _make_definition([
            {"name": "diagnose", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "d"},
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "a"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"summary": "ok"}))

        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "paused"
        assert state.steps["a"].status == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_resume_approved(self) -> None:
        """Test resuming after approval."""
        defn = _make_definition([
            {"name": "diagnose", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "d"},
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "a"},
            {"name": "fix", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix", "output_key": "f"},
        ])
        call_count = 0

        async def mock_run(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response({"summary": "broken"})
            return _make_response({"summary": "fixed"})

        client = AsyncMock()
        client.run = mock_run

        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()
        assert state.status == "paused"

        state = await executor.resume(state.workflow_id, approved=True)
        assert state.status == "completed"
        assert state.steps["a"].output["approved"] is True

    @pytest.mark.asyncio
    async def test_resume_rejected(self) -> None:
        """Test rejecting an approval."""
        defn = _make_definition([
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "a"},
        ])
        executor = GraphExecutor(defn, _mock_registry(), client_factory=lambda _: AsyncMock())
        state = await executor.run()
        assert state.status == "paused"

        state = await executor.resume(state.workflow_id, approved=False)
        assert state.status == "failed"
        assert state.steps["a"].error == "Approval rejected by human"


class TestGraphExecutorAdvisory:
    """Advisory mode tests for GraphExecutor."""

    @pytest.mark.asyncio
    async def test_advisory_skips_approval(self) -> None:
        """Test advisory mode skips approval steps."""
        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "r1"},
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "a"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"summary": "ok"}))

        advisory = AdvisoryEnforcer(enabled=True)
        executor = GraphExecutor(
            defn, _mock_registry(), client_factory=lambda _: client, advisory=advisory,
        )
        state = await executor.run()

        assert state.status == "completed"
        assert state.steps["a"].status == "skipped"

    @pytest.mark.asyncio
    async def test_advisory_annotates_output(self) -> None:
        """Test advisory mode annotates agent output."""
        defn = _make_definition([
            {"name": "s1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "r1"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"summary": "ok"}))

        advisory = AdvisoryEnforcer(enabled=True)
        executor = GraphExecutor(
            defn, _mock_registry(), client_factory=lambda _: client, advisory=advisory,
        )
        state = await executor.run()

        assert state.steps["r1"].output["advisory"] is True
