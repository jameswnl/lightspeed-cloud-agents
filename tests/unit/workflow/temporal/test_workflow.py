"""Unit tests for AgentWorkflow Temporal class (TDD — RED phase).

These tests use Temporal's WorkflowEnvironment test harness.
They define the expected behavior; the workflow class is implemented
to make them pass.
"""

from __future__ import annotations

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
)
from agents.workflow.temporal_models import (
    ProviderConfig,
    WorkflowInput,
    WorkflowStatus,
)
from agents.workflow.temporal_workflow import AgentWorkflow


def _make_input(steps: list[dict], input_prompt: str | None = None) -> WorkflowInput:
    """Create a WorkflowInput with the given steps."""
    return WorkflowInput(
        definition={
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test-wf"},
            "spec": {"steps": steps},
        },
        input_prompt=input_prompt,
        workflow_id="wf-test-1",
        provider=ProviderConfig(name="openai", model="gpt-4", credentials_secret="test-key"),
    )


@pytest.fixture
async def env():
    """Create a Temporal test environment with time skipping."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env




class TestSequentialWorkflow:
    """Tests for sequential step execution."""

    @pytest.mark.asyncio
    async def test_two_steps_complete_in_order(self, env: WorkflowEnvironment) -> None:
        """Two agent steps run sequentially and both complete."""
        steps = [
            {"name": "step1", "type": "agent", "output_key": "r1",
             "prompt": "diagnose", "runtime": "sandbox", "spawn": "ephemeral"},
            {"name": "step2", "type": "agent", "output_key": "r2",
             "prompt": "fix", "runtime": "sandbox", "spawn": "ephemeral"},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],

        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-seq-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
        assert result.steps["r2"].status == "completed"


class TestConditionEvaluation:
    """Tests for step condition evaluation."""

    @pytest.mark.asyncio
    async def test_false_condition_skips_step(self, env: WorkflowEnvironment) -> None:
        """Step with false condition is skipped."""
        steps = [
            {"name": "step1", "type": "agent", "output_key": "r1",
             "prompt": "check", "runtime": "sandbox", "spawn": "ephemeral"},
            {"name": "step2", "type": "agent", "output_key": "r2",
             "prompt": "fix", "runtime": "sandbox", "spawn": "ephemeral",
             "condition": "steps.r1.output.needs_fix == true"},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],

        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-cond-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
        assert result.steps["r2"].status == "skipped"


class TestConditionFailClosed:
    """Tests that invalid conditions fail closed (skip step)."""

    @pytest.mark.asyncio
    async def test_invalid_condition_skips_step(self, env: WorkflowEnvironment) -> None:
        """Unparseable condition skips the step instead of running it."""
        steps = [
            {"name": "step1", "type": "agent", "output_key": "r1",
             "prompt": "check", "runtime": "sandbox", "spawn": "ephemeral"},
            {"name": "step2", "type": "agent", "output_key": "r2",
             "prompt": "fix", "runtime": "sandbox", "spawn": "ephemeral",
             "condition": "this is not a valid condition expression"},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],

        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-failclosed-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
        assert result.steps["r2"].status == "skipped"


class TestParallelGroup:
    """Tests for parallel group execution."""

    @pytest.mark.asyncio
    async def test_parallel_steps_run(self, env: WorkflowEnvironment) -> None:
        """Steps in the same parallel_group run concurrently."""
        steps = [
            {"name": "a", "type": "agent", "output_key": "ra",
             "prompt": "check-a", "runtime": "sandbox", "spawn": "ephemeral",
             "parallel_group": "diag"},
            {"name": "b", "type": "agent", "output_key": "rb",
             "prompt": "check-b", "runtime": "sandbox", "spawn": "ephemeral",
             "parallel_group": "diag"},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],

        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-parallel-1",
                task_queue="test-q",
            )

        assert result.steps["ra"].status == "completed"
        assert result.steps["rb"].status == "completed"


class TestApprovalFlow:
    """Tests for human approval via signals."""

    @pytest.mark.asyncio
    async def test_approval_signal_resumes_workflow(self, env: WorkflowEnvironment) -> None:
        """Sending an approve signal unblocks a paused workflow."""
        steps = [
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "approval",
             "timeout_seconds": 86400},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[build_escalation_activity],

        ):
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-approve-1",
                task_queue="test-q",
            )

            await handle.signal(AgentWorkflow.approve, args=["approve", "approved", None])
            result = await handle.result()

        assert result.steps["approval"].status == "completed"
        assert result.steps["approval"].output["approved"] is True

    @pytest.mark.asyncio
    async def test_approval_timeout_produces_denied(self, env: WorkflowEnvironment) -> None:
        """No signal within timeout produces denied status."""
        steps = [
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "approval",
             "timeout_seconds": 2},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[build_escalation_activity],

        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-timeout-1",
                task_queue="test-q",
            )

        assert result.steps["approval"].status == "denied"
        assert result.steps["approval"].output["reason"] == "timeout"


class TestQueryStatus:
    """Tests for workflow status queries."""

    @pytest.mark.asyncio
    async def test_query_returns_current_status(self, env: WorkflowEnvironment) -> None:
        """Query returns step results and events."""
        steps = [
            {"name": "approve", "type": "human-approval",
             "message": "OK?", "output_key": "approval"},
        ]

        async with Worker(
            env.client, task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[build_escalation_activity],

        ):
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                _make_input(steps),
                id="wf-query-1",
                task_queue="test-q",
            )

            status = await handle.query(AgentWorkflow.get_status)
            assert isinstance(status, WorkflowStatus)
            assert len(status.events) > 0

            await handle.signal(AgentWorkflow.approve, args=["approve", "approved", None])
            await handle.result()
