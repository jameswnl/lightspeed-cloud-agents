"""Integration tests for LocalWorkflowRunner — full workflow execution.

Tests the complete flow: factory → LocalWorkflowRunner → pydantic-graph → step_runner.
Uses mock spawner and mock RunStateStore (no real PostgreSQL or containers).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


def _make_workflow_input(
    steps: list[dict[str, Any]],
    *,
    workflow_id: str = "",
    approval_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full workflow run input."""
    input_data: dict[str, Any] = {
        "definition": {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "integration-test"},
            "spec": {"steps": steps},
        },
        "provider": {
            "name": "openai",
            "model": "gpt-4o",
            "credentials_secret": "openai-api-key",
        },
        "sandbox_image": "sandbox:latest",
    }
    if workflow_id:
        input_data["workflow_id"] = workflow_id
    if approval_policy:
        input_data["approval_policy"] = approval_policy
    return input_data


@pytest.fixture(name="mock_spawner")
def mock_spawner_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock spawner that returns stub results."""
    spawner = mocker.AsyncMock()
    spawner.spawn.return_value = "http://pod-1:8080"
    spawner.wait_ready.return_value = True
    return spawner


@pytest.fixture(name="mock_store")
def mock_store_fixture(mocker: MockerFixture) -> AsyncMock:
    """Mock RunStateStore."""
    store = mocker.AsyncMock()
    store.create = mocker.AsyncMock()
    store.get = mocker.AsyncMock(return_value=None)
    store.update_step = mocker.AsyncMock()
    store.append_event = mocker.AsyncMock()
    store.set_paused = mocker.AsyncMock()
    store.resume = mocker.AsyncMock()
    store.mark_terminal = mocker.AsyncMock()
    store.update_workflow_context = mocker.AsyncMock()
    store.list_paused = mocker.AsyncMock(return_value=[])
    return store


@pytest.fixture(name="executor")
def executor_fixture(mock_spawner: AsyncMock, mock_store: AsyncMock) -> Any:
    """Create a LocalWorkflowRunner with mocked dependencies."""
    from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

    return LocalWorkflowRunner(
        spawner=mock_spawner,
        run_state_store=mock_store,
    )


class TestSingleAgentStep:
    """Integration: single agent step workflow."""

    @pytest.mark.asyncio
    async def test_single_step_completes(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Single agent step runs and completes."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "all good"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_workflow_input([
            {"name": "diagnose", "type": "agent", "prompt": "Check", "output_key": "diagnosis"},
        ])

        wf_id = await executor.start(input_data)
        assert wf_id.startswith("wf-")

        # Wait for async execution
        await asyncio.sleep(0.1)

        mock_store.mark_terminal.assert_called_with(wf_id, "completed")


class TestMultiStepWorkflow:
    """Integration: multi-step sequential workflow."""

    @pytest.mark.asyncio
    async def test_two_steps_execute_sequentially(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Two agent steps execute in order."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        call_order = []

        async def mock_run(step_input):
            step_name = step_input.step_name
            call_order.append(step_name)
            return StepResult(
                status="completed",
                output={"step": step_name},
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = mock_run
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_workflow_input([
            {"name": "diagnose", "type": "agent", "prompt": "Diagnose", "output_key": "diag"},
            {"name": "fix", "type": "agent", "prompt": "Fix", "output_key": "fix_result"},
        ])

        await executor.start(input_data)
        await asyncio.sleep(0.2)

        assert call_order == ["diagnose", "fix"]


class TestApprovalGate:
    """Integration: workflow with human approval gate."""

    @pytest.mark.asyncio
    async def test_auto_approve_completes(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Workflow with auto_approve=True completes without pausing."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_workflow_input(
            [
                {"name": "diagnose", "type": "agent", "prompt": "Check", "output_key": "diag"},
                {"name": "approve", "type": "human-approval", "output_key": "approval", "message": "OK?"},
                {"name": "fix", "type": "agent", "prompt": "Fix", "output_key": "fix_result"},
            ],
            approval_policy={"auto_approve": True},
        )

        await executor.start(input_data)
        await asyncio.sleep(0.2)

        mock_store.mark_terminal.assert_called()

    @pytest.mark.asyncio
    async def test_approval_pauses_workflow(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Workflow pauses at approval step when no auto_approve."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_workflow_input([
            {"name": "diagnose", "type": "agent", "prompt": "Check", "output_key": "diag"},
            {"name": "approve", "type": "human-approval", "output_key": "approval", "message": "OK?"},
            {"name": "fix", "type": "agent", "prompt": "Fix", "output_key": "fix_result"},
        ])

        await executor.start(input_data)
        await asyncio.sleep(0.2)

        mock_store.set_paused.assert_called()
        # fix step should NOT have been called
        mock_store.mark_terminal.assert_not_called()


class TestConditionEvaluation:
    """Integration: condition-based step skipping."""

    @pytest.mark.asyncio
    async def test_false_condition_skips_step(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Step with false condition is skipped."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        call_count = 0

        async def mock_run(step_input):
            nonlocal call_count
            call_count += 1
            return StepResult(
                status="completed",
                output={"severity": "low"},
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = mock_run
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input_data = _make_workflow_input([
            {"name": "diagnose", "type": "agent", "prompt": "Check", "output_key": "diagnosis"},
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix",
                "output_key": "fix_result",
                "condition": "steps.diagnosis.output.severity == 'high'",
            },
        ])

        await executor.start(input_data)
        await asyncio.sleep(0.2)

        # Only diagnose should have been called, fix should be skipped
        assert call_count == 1


class TestDuplicateWorkflowId:
    """Integration: duplicate workflow ID rejection."""

    @pytest.mark.asyncio
    async def test_duplicate_id_rejected(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Submitting same workflow_id twice raises ValueError."""
        mock_store.create.side_effect = ValueError("already exists")

        input_data = _make_workflow_input(
            [{"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"}],
            workflow_id="wf-dup-test",
        )

        with pytest.raises(ValueError, match="already exists"):
            await executor.start(input_data)


class TestFactoryIntegration:
    """Integration: factory creates correct executor."""

    def test_factory_local_creates_runner(
        self, mocker: MockerFixture
    ) -> None:
        """Factory with WORKFLOW_ENGINE=local creates LocalWorkflowRunner."""
        mocker.patch.dict(os.environ, {"WORKFLOW_ENGINE": "local"}, clear=False)

        from cloud_agents.workflow.executor.factory import create_runner
        from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

        runner = create_runner()
        assert isinstance(runner, LocalWorkflowRunner)

    def test_factory_temporal_requires_url(
        self, mocker: MockerFixture
    ) -> None:
        """Factory with WORKFLOW_ENGINE=temporal requires TEMPORAL_URL."""
        mocker.patch.dict(
            os.environ,
            {"WORKFLOW_ENGINE": "temporal", "TEMPORAL_URL": ""},
            clear=False,
        )

        from cloud_agents.workflow.executor.factory import create_runner

        with pytest.raises(ValueError, match="TEMPORAL_URL"):
            create_runner()


class TestParallelWorkflows:
    """Integration: multiple concurrent workflows."""

    @pytest.mark.asyncio
    async def test_parallel_workflows(
        self,
        executor: Any,
        mock_store: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Multiple workflows can execute concurrently."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        input1 = _make_workflow_input(
            [{"name": "s1", "type": "agent", "prompt": "test1", "output_key": "r1"}],
            workflow_id="wf-parallel-1",
        )
        input2 = _make_workflow_input(
            [{"name": "s1", "type": "agent", "prompt": "test2", "output_key": "r1"}],
            workflow_id="wf-parallel-2",
        )

        wf1 = await executor.start(input1)
        wf2 = await executor.start(input2)

        assert wf1 != wf2
        await asyncio.sleep(0.2)

        # Both should complete
        terminal_calls = [
            c for c in mock_store.mark_terminal.call_args_list
            if c.args[1] == "completed"
        ]
        assert len(terminal_calls) == 2
