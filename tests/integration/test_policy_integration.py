"""Integration tests for Temporal workflow policy combinations.

Uses WorkflowEnvironment.start_time_skipping() to test policy
features working together in realistic multi-step scenarios.
"""

from __future__ import annotations

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
    send_approval_notification,
)
from agents.workflow.temporal_models import (
    ProviderConfig,
    WorkflowInput,
)
from agents.workflow.temporal_workflow import AgentWorkflow


def _make_input(steps: list[dict], **kwargs) -> WorkflowInput:
    """Create a WorkflowInput with the given steps and overrides."""
    return WorkflowInput(
        definition={
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test-wf"},
            "spec": {"steps": steps},
        },
        workflow_id="wf-int-1",
        provider=ProviderConfig(
            name="openai", model="gpt-4", credentials_secret="test-key"
        ),
        **kwargs,
    )


@pytest.fixture
async def env():
    """Create a Temporal test environment with time skipping."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


class TestMixedRiskLevels:
    """Tests for workflows with mixed auto-approve and manual steps."""

    @pytest.mark.asyncio
    async def test_low_risk_auto_high_risk_manual(
        self, env: WorkflowEnvironment
    ) -> None:
        """Low-risk step auto-approves, high-risk times out."""
        steps = [
            {
                "name": "check",
                "type": "agent",
                "output_key": "r1",
                "prompt": "diagnose",
                "runtime": "sandbox",
                "spawn": "ephemeral",
            },
            {
                "name": "auto-approve",
                "type": "human-approval",
                "message": "Quick check",
                "output_key": "a1",
                "risk_level": "low",
                "timeout_seconds": 2,
            },
            {
                "name": "manual-approve",
                "type": "human-approval",
                "message": "Risky action",
                "output_key": "a2",
                "risk_level": "high",
                "timeout_seconds": 2,
            },
        ]
        wf_input = _make_input(
            steps,
            approval_policy={"auto_approve_risk_levels": ["low"]},
        )

        async with Worker(
            env.client,
            task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[
                run_sandbox_step,
                build_escalation_activity,
                send_approval_notification,
            ],
        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                wf_input,
                id="wf-mixed-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
        assert result.steps["a1"].status == "completed"
        assert result.steps["a1"].output["auto_approved"] is True
        assert result.steps["a2"].status == "denied"


class TestAdvisoryEndToEnd:
    """Tests for advisory mode across a full workflow."""

    @pytest.mark.asyncio
    async def test_advisory_skips_approval_marks_output(
        self,
        env: WorkflowEnvironment,
    ) -> None:
        """Advisory mode skips approval and marks agent output."""
        steps = [
            {
                "name": "diag",
                "type": "agent",
                "output_key": "r1",
                "prompt": "diagnose",
                "runtime": "sandbox",
                "spawn": "ephemeral",
            },
            {
                "name": "approve",
                "type": "human-approval",
                "message": "Apply fix?",
                "output_key": "a1",
                "timeout_seconds": 2,
            },
        ]
        wf_input = _make_input(steps, advisory=True)

        async with Worker(
            env.client,
            task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[
                run_sandbox_step,
                build_escalation_activity,
                send_approval_notification,
            ],
        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                wf_input,
                id="wf-adv-int-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
        assert result.steps["r1"].output.get("advisory") is True
        assert result.steps["a1"].status == "completed"
        assert result.steps["a1"].output.get("advisory") is True


class TestServiceAccountPassthrough:
    """Tests for service_account flowing through to sandbox."""

    @pytest.mark.asyncio
    async def test_permissions_in_activity_dispatch(
        self,
        env: WorkflowEnvironment,
    ) -> None:
        """Step with permissions dispatches with service_account in context."""
        steps = [
            {
                "name": "restricted",
                "type": "agent",
                "output_key": "r1",
                "prompt": "check",
                "runtime": "sandbox",
                "spawn": "ephemeral",
                "permissions": {
                    "service_account": "readonly-sa",
                    "timeout_seconds": 120,
                },
            },
        ]
        wf_input = _make_input(steps)

        async with Worker(
            env.client,
            task_queue="test-q",
            workflows=[AgentWorkflow],
            activities=[run_sandbox_step, build_escalation_activity],
        ):
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                wf_input,
                id="wf-perm-int-1",
                task_queue="test-q",
            )

        assert result.steps["r1"].status == "completed"
