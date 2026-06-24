"""Unit tests for WorkflowExecutor."""

import pytest
from unittest.mock import AsyncMock, patch

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.advisory import AdvisoryEnforcer
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.executor import WorkflowExecutor


def _make_definition(steps: list[dict]) -> WorkflowDefinition:
    """Create a WorkflowDefinition from step dicts."""
    step_specs = [WorkflowStepSpec(**{**s, "spawn": s.get("spawn", "pre-deployed")}) for s in steps]
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
        """Test that an agent failure (with max_retries=1) fails the workflow."""
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
        assert "Retries exhausted" in state.steps["result"].error


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


class TestWorkflowExecutorRetry:
    """Tests for retry with context and escalation."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        """Test that a step retries and succeeds on the second attempt."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix it", "output_key": "result", "max_retries": 3},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            Exception("Agent down"),
            _make_agent_response({"summary": "Fixed on retry"}),
        ])

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "completed"
        assert state.steps["result"].status == "completed"
        assert client.run.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_generates_escalation(self) -> None:
        """Test that exhausted retries generate an escalation handoff."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix it", "output_key": "result", "max_retries": 2},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Always fails"))

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.status == "failed"
        assert state.steps["result"].status == "failed"
        assert "Retries exhausted" in state.steps["result"].error
        assert state.steps["result"].output is not None
        assert state.steps["result"].output["failure_history"] is not None
        assert len(state.steps["result"].output["failure_history"]) == 2
        assert client.run.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_prompt_includes_failure_history(self) -> None:
        """Test that retry prompts include previous failure context."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Original prompt", "output_key": "result", "max_retries": 2},
        ])
        prompts_received = []
        call_count = 0

        async def capturing_run(prompt, **kwargs):
            nonlocal call_count
            prompts_received.append(prompt)
            call_count += 1
            if call_count == 1:
                raise Exception("First failure")
            return _make_agent_response({"summary": "Fixed"})

        client = AsyncMock()
        client.run = capturing_run

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        await executor.run()

        assert len(prompts_received) == 2
        assert "Original prompt" in prompts_received[0]
        assert "PREVIOUS ATTEMPTS" in prompts_received[1]
        assert "First failure" in prompts_received[1]


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


class TestWorkflowExecutorSpawner:
    """Tests for on-demand spawning integration."""

    @pytest.mark.asyncio
    async def test_on_demand_spawn_uses_unique_name(self) -> None:
        """Test that on-demand spawn generates a unique name and cleans up with it."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Do something", "output_key": "result",
             "spawn": "on-demand"},
        ])
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)
        spawner.destroy = AsyncMock()

        mock_client = AsyncMock()
        mock_client.run = AsyncMock(return_value=_make_agent_response({"summary": "Done"}))

        executor = WorkflowExecutor(
            defn, _mock_registry(),
            spawner=spawner,
        )
        with patch("agents.workflow.executor.RemoteAgentClient", return_value=mock_client):
            state = await executor.run()

        assert state.status == "completed"
        spawner.spawn.assert_called_once()
        spawn_name = spawner.spawn.call_args[0][0]
        assert spawn_name.startswith("diagnostic-agent-")
        assert len(spawn_name) > len("diagnostic-agent-")
        spawner.destroy.assert_called_once_with(spawn_name)

    @pytest.mark.asyncio
    async def test_on_demand_spawn_cleanup_on_failure(self) -> None:
        """Test that spawned agent is cleaned up even when the step fails."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Do something", "output_key": "result",
             "spawn": "on-demand"},
        ])
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)
        spawner.destroy = AsyncMock()

        mock_client = AsyncMock()
        mock_client.run = AsyncMock(side_effect=Exception("Agent crashed"))

        executor = WorkflowExecutor(
            defn, _mock_registry(),
            spawner=spawner,
        )
        with patch("agents.workflow.executor.RemoteAgentClient", return_value=mock_client):
            state = await executor.run()

        spawner.destroy.assert_called_once()
        destroy_name = spawner.destroy.call_args[0][0]
        assert destroy_name.startswith("diagnostic-agent-")


class TestWorkflowExecutorAdvisory:
    """Tests for advisory mode in workflow execution."""

    @pytest.mark.asyncio
    async def test_advisory_skips_approval_steps(self) -> None:
        """Test that advisory mode skips human-approval steps."""
        defn = _make_definition([
            {"name": "diagnose", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "diagnosis"},
            {"name": "approve", "type": "human-approval",
             "message": "Approve?", "output_key": "approval"},
            {"name": "fix", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Fix", "output_key": "fix"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(side_effect=[
            _make_agent_response({"summary": "issues found"}),
            _make_agent_response({"summary": "fixed"}),
        ])

        advisory = AdvisoryEnforcer(enabled=True)
        executor = WorkflowExecutor(
            defn, _mock_registry(),
            client_factory=lambda _: client,
            advisory=advisory,
        )
        state = await executor.run()

        assert state.status == "completed"
        assert state.steps["approval"].status == "skipped"
        assert state.steps["approval"].output["advisory"] is True
        assert state.steps["fix"].status == "completed"

    @pytest.mark.asyncio
    async def test_advisory_annotates_prompt(self) -> None:
        """Test that advisory mode appends advisory suffix to prompts."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check hosts", "output_key": "result"},
        ])
        prompts_received = []

        async def capturing_run(prompt, **kwargs):
            prompts_received.append(prompt)
            return _make_agent_response({"summary": "ok"})

        client = AsyncMock()
        client.run = capturing_run

        advisory = AdvisoryEnforcer(enabled=True)
        executor = WorkflowExecutor(
            defn, _mock_registry(),
            client_factory=lambda _: client,
            advisory=advisory,
        )
        await executor.run()

        assert len(prompts_received) == 1
        assert "ADVISORY MODE" in prompts_received[0]
        assert "Check hosts" in prompts_received[0]

    @pytest.mark.asyncio
    async def test_advisory_annotates_output(self) -> None:
        """Test that advisory mode adds advisory marker to output."""
        defn = _make_definition([
            {"name": "step1", "type": "agent", "agent": "diagnostic-agent",
             "prompt": "Check", "output_key": "result"},
        ])
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_agent_response({"summary": "ok"}))

        advisory = AdvisoryEnforcer(enabled=True)
        executor = WorkflowExecutor(
            defn, _mock_registry(),
            client_factory=lambda _: client,
            advisory=advisory,
        )
        state = await executor.run()

        assert state.steps["result"].output["advisory"] is True
        assert state.steps["result"].output["summary"] == "ok"

    @pytest.mark.asyncio
    async def test_advisory_from_metadata(self) -> None:
        """Test that advisory mode is auto-detected from workflow metadata."""
        step_specs = [WorkflowStepSpec(
            name="s1", type="agent", agent="diagnostic-agent",
            prompt="Check", output_key="r1", spawn="pre-deployed",
        )]
        defn = WorkflowDefinition(
            apiVersion="v1", kind="AgentWorkflow",
            metadata={"name": "test", "mode": "advisory"},
            spec=WorkflowSpec(steps=step_specs),
        )
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_agent_response({"summary": "ok"}))

        executor = WorkflowExecutor(defn, _mock_registry(), client_factory=lambda _: client)
        state = await executor.run()

        assert state.steps["r1"].output["advisory"] is True
