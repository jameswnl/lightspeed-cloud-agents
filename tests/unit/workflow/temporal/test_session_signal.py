"""Unit tests for session_result Temporal signal (T15 Phase 3, Task 2).

Tests that the AgentWorkflow can receive session result signals and
expose them via query.
"""

from __future__ import annotations

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cloud_agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
)
from cloud_agents.workflow.temporal_models import (
    ProviderConfig,
    WorkflowInput,
)
from cloud_agents.workflow.temporal_workflow import AgentWorkflow


def _make_input(steps: list[dict]) -> WorkflowInput:
    """Create a WorkflowInput with the given steps."""
    return WorkflowInput(
        definition={
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test-wf"},
            "spec": {"steps": steps},
        },
        workflow_id="wf-test-signal",
        provider=ProviderConfig(
            name="openai", model="gpt-4", credentials_secret="test-key"
        ),
    )


@pytest.fixture
async def env():
    """Create a Temporal test environment with time skipping."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


class TestSessionResultSignal:
    """Tests for session_result signal and query."""

    @pytest.mark.asyncio
    async def test_session_result_signal_stores_data(self, env: WorkflowEnvironment) -> None:
        """session_result signal stores result data queryable via get_session_results."""

        async def mock_activity(input: dict) -> dict:
            return {"status": "completed", "output": {"result": "ok"}}

        async with Worker(
            env.client,
            task_queue="test-session-signal",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],
        ):
            steps = [
                {
                    "name": "approval-gate",
                    "type": "human-approval",
                    "output_key": "gate",
                    "message": "Approve?",
                    "timeout_seconds": 30,
                }
            ]
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-signal-test-1",
                task_queue="test-session-signal",
            )

            # Send a session result signal
            await handle.signal(
                AgentWorkflow.session_result,
                args=["cli-sess-abc123", {"output": "agent completed task"}],
            )

            # Query for session results
            results = await handle.query(AgentWorkflow.get_session_results)
            assert "cli-sess-abc123" in results
            assert results["cli-sess-abc123"]["output"] == "agent completed task"

            # Approve to let workflow finish
            await handle.signal(
                AgentWorkflow.approve,
                args=["approval-gate", "approved"],
            )
            await handle.result()

    @pytest.mark.asyncio
    async def test_multiple_session_results(self, env: WorkflowEnvironment) -> None:
        """Multiple session_result signals are stored independently."""

        async with Worker(
            env.client,
            task_queue="test-multi-signal",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],
        ):
            steps = [
                {
                    "name": "gate",
                    "type": "human-approval",
                    "output_key": "gate",
                    "message": "Approve?",
                    "timeout_seconds": 30,
                }
            ]
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-signal-test-2",
                task_queue="test-multi-signal",
            )

            # Send two different session results
            await handle.signal(
                AgentWorkflow.session_result,
                args=["sess-1", {"output": "result-1"}],
            )
            await handle.signal(
                AgentWorkflow.session_result,
                args=["sess-2", {"output": "result-2"}],
            )

            results = await handle.query(AgentWorkflow.get_session_results)
            assert len(results) == 2
            assert results["sess-1"]["output"] == "result-1"
            assert results["sess-2"]["output"] == "result-2"

            # Cleanup
            await handle.signal(AgentWorkflow.approve, args=["gate", "approved"])
            await handle.result()

    @pytest.mark.asyncio
    async def test_session_results_empty_by_default(self, env: WorkflowEnvironment) -> None:
        """get_session_results returns empty dict when no signals sent."""

        async def mock_activity(input: dict) -> dict:
            return {"status": "completed", "output": {"result": "ok"}}

        async with Worker(
            env.client,
            task_queue="test-empty-signal",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],
        ):
            steps = [
                {
                    "name": "gate",
                    "type": "human-approval",
                    "output_key": "gate",
                    "message": "Approve?",
                    "timeout_seconds": 30,
                }
            ]
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-signal-test-3",
                task_queue="test-empty-signal",
            )

            results = await handle.query(AgentWorkflow.get_session_results)
            assert results == {}

            # Cleanup
            await handle.signal(AgentWorkflow.approve, args=["gate", "approved"])
            await handle.result()
