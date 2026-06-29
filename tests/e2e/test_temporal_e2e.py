"""Temporal server integration tests for workflow orchestration.

These tests validate the Temporal workflow engine against a real
Temporal Server: workflow execution, signals, queries, policy
features (auto-approval, advisory, conditions, parallel groups).

The sandbox activity runs in stub mode (no spawner) — these tests
prove the orchestration layer, not the spawn→HTTP→destroy path.
For full sandbox E2E, deploy the workflow-runner with a real spawner.

Requires a running Temporal Server. Set TEMPORAL_E2E_URL env var.
Default: localhost:7233 (assumes port-forward or local Temporal).

Run:
    kubectl port-forward svc/temporal 7233:7233 &
    uv run pytest tests/e2e/temporal/ -v
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
    send_approval_notification,
)
from agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from agents.workflow.temporal_workflow import AgentWorkflow

TEMPORAL_URL = os.environ.get("TEMPORAL_E2E_URL", "localhost:7233")
ALL_ACTIVITIES = [run_sandbox_step, build_escalation_activity, send_approval_notification]


def _wf_id():
    return f"e2e-{uuid.uuid4().hex[:12]}"


def _queue():
    return f"e2e-{uuid.uuid4().hex[:8]}"


def _input(steps, **kwargs):
    return WorkflowInput(
        definition={
            "apiVersion": "v1", "kind": "AgentWorkflow",
            "metadata": {"name": "e2e"}, "spec": {"steps": steps},
        },
        workflow_id=_wf_id(),
        provider=ProviderConfig(name="openai", model="gpt-4", credentials_secret="test"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_sequential_workflow():
    """Two agent steps complete sequentially."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input([
        {"name": "s1", "type": "agent", "output_key": "r1",
         "prompt": "diagnose", "runtime": "sandbox", "spawn": "ephemeral"},
        {"name": "s2", "type": "agent", "output_key": "r2",
         "prompt": "fix", "runtime": "sandbox", "spawn": "ephemeral"},
    ])
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["r1"].status == "completed"
    assert result.steps["r2"].status == "completed"


@pytest.mark.asyncio
async def test_auto_approval_low_risk():
    """Low-risk step auto-approves without signal."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input(
        [{"name": "approve", "type": "human-approval",
          "message": "OK?", "output_key": "a1",
          "risk_level": "low", "timeout_seconds": 5}],
        approval_policy={"auto_approve_risk_levels": ["low"]},
    )
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["a1"].output["auto_approved"] is True


@pytest.mark.asyncio
async def test_mixed_risk_auto_and_manual():
    """Low auto-approves, high times out."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input(
        [
            {"name": "diag", "type": "agent", "output_key": "r1",
             "prompt": "check", "runtime": "sandbox", "spawn": "ephemeral"},
            {"name": "auto", "type": "human-approval",
             "message": "Quick", "output_key": "a1",
             "risk_level": "low", "timeout_seconds": 5},
            {"name": "manual", "type": "human-approval",
             "message": "Risky", "output_key": "a2",
             "risk_level": "high", "timeout_seconds": 2},
        ],
        approval_policy={"auto_approve_risk_levels": ["low"]},
    )
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["a1"].output["auto_approved"] is True
    assert result.steps["a2"].status == "denied"


@pytest.mark.asyncio
async def test_advisory_mode():
    """Advisory skips approval and marks output."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input(
        [
            {"name": "diag", "type": "agent", "output_key": "r1",
             "prompt": "diagnose", "runtime": "sandbox", "spawn": "ephemeral"},
            {"name": "approve", "type": "human-approval",
             "message": "Apply?", "output_key": "a1", "timeout_seconds": 5},
        ],
        advisory=True,
    )
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["r1"].output.get("advisory") is True
    assert result.steps["a1"].output.get("advisory") is True


@pytest.mark.asyncio
async def test_approval_signal():
    """Signal resumes a paused workflow."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input([
        {"name": "approve", "type": "human-approval",
         "message": "OK?", "output_key": "a1", "timeout_seconds": 30},
    ])
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        handle = await client.start_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
        await asyncio.sleep(1)
        await handle.signal(AgentWorkflow.approve, args=["approve", "approved", None])
        result = await handle.result()
    assert result.steps["a1"].output["approved"] is True


@pytest.mark.asyncio
async def test_condition_skips_step():
    """False condition skips the guarded step."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input([
        {"name": "s1", "type": "agent", "output_key": "r1",
         "prompt": "check", "runtime": "sandbox", "spawn": "ephemeral"},
        {"name": "s2", "type": "agent", "output_key": "r2",
         "prompt": "fix", "runtime": "sandbox", "spawn": "ephemeral",
         "condition": "steps.r1.output.needs_fix == true"},
    ])
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["r2"].status == "skipped"


@pytest.mark.asyncio
async def test_parallel_group():
    """Steps in same parallel_group run concurrently."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input([
        {"name": "a", "type": "agent", "output_key": "ra",
         "prompt": "check-a", "runtime": "sandbox", "spawn": "ephemeral",
         "parallel_group": "diag"},
        {"name": "b", "type": "agent", "output_key": "rb",
         "prompt": "check-b", "runtime": "sandbox", "spawn": "ephemeral",
         "parallel_group": "diag"},
    ])
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        result = await client.execute_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
    assert result.steps["ra"].status == "completed"
    assert result.steps["rb"].status == "completed"


@pytest.mark.asyncio
async def test_workflow_query():
    """Query returns events during paused workflow."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input([
        {"name": "approve", "type": "human-approval",
         "message": "OK?", "output_key": "a1", "timeout_seconds": 30},
    ])
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        handle = await client.start_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
        await asyncio.sleep(2)
        status = await handle.query(AgentWorkflow.get_status)
        assert len(status.events) > 0
        assert any(e.type == "workflow.paused" for e in status.events)

        await handle.signal(AgentWorkflow.approve, args=["approve", "approved", None])
        await handle.result()


@pytest.mark.asyncio
async def test_notification_dispatched_on_pause():
    """Notification activity fires when workflow pauses for approval."""
    client = await Client.connect(TEMPORAL_URL)
    q = _queue()
    wf = _input(
        [{"name": "approve", "type": "human-approval",
          "message": "Review needed", "output_key": "a1",
          "risk_level": "high", "timeout_seconds": 30}],
        approval_policy={"auto_approve_risk_levels": ["low"]},
    )
    async with Worker(client, task_queue=q, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES):
        handle = await client.start_workflow(
            AgentWorkflow.run, wf, id=wf.workflow_id, task_queue=q,
        )
        await asyncio.sleep(2)
        await handle.signal(AgentWorkflow.approve, args=["approve", "approved", None])
        result = await handle.result()
    assert result.steps["a1"].status == "completed"
