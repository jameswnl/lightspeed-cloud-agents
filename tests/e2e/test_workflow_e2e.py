"""E2E test: diagnose-fix workflow through real Temporal server.

Exercises the full 4-step workflow (diagnose → approve → fix → verify)
using stub-mode activities (no LLM or sandbox containers). Proves the
orchestration engine works end-to-end: step chaining, auto-approve,
condition evaluation, prompt interpolation, event emission.

Requires a running Temporal Server. Set TEMPORAL_E2E_URL env var.
Default: localhost:7233.

Run:
    TEMPORAL_E2E_URL=localhost:7233 uv run pytest tests/e2e/test_workflow_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from temporalio.client import Client
from temporalio.worker import Worker

from cloud_agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
    send_approval_notification,
)
from cloud_agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from cloud_agents.workflow.temporal_workflow import AgentWorkflow

TEMPORAL_URL = os.environ.get("TEMPORAL_E2E_URL", "localhost:7233")
WORKFLOW_YAML = Path(__file__).parents[2] / "examples" / "workflow-definitions" / "diagnose-fix-workflow.yaml"
ALL_ACTIVITIES = [run_sandbox_step, build_escalation_activity, send_approval_notification]


@pytest.mark.asyncio
async def test_diagnose_fix_workflow_e2e():
    """Full 4-step diagnose-fix workflow completes with auto-approve."""
    assert WORKFLOW_YAML.exists(), f"Workflow YAML not found: {WORKFLOW_YAML}"

    with open(WORKFLOW_YAML) as f:
        definition = yaml.safe_load(f)

    client = await Client.connect(TEMPORAL_URL)
    queue = f"e2e-{uuid.uuid4().hex[:8]}"
    wf_id = f"e2e-diagnose-fix-{uuid.uuid4().hex[:8]}"

    wf_input = WorkflowInput(
        definition=definition,
        workflow_id=wf_id,
        provider=ProviderConfig(name="openai", model="gpt-4", credentials_secret="test"),
        approval_policy={"auto_approve_risk_levels": ["low", "medium", "high", "critical"]},
    )

    async with Worker(
        client, task_queue=queue, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES
    ):
        result = await client.execute_workflow(
            AgentWorkflow.run,
            wf_input,
            id=wf_id,
            task_queue=queue,
            execution_timeout=timedelta(seconds=120),
        )

    # All 4 steps completed
    assert "diagnosis" in result.steps, "diagnosis step missing"
    assert "approval" in result.steps, "approval step missing"
    assert "fix" in result.steps, "fix step missing"
    assert "verification" in result.steps, "verification step missing"

    assert result.steps["diagnosis"].status == "completed"
    assert result.steps["approval"].status == "completed"
    assert result.steps["fix"].status == "completed"
    assert result.steps["verification"].status == "completed"

    # Approval was auto-approved
    assert result.steps["approval"].output["auto_approved"] is True

    # Fix ran because condition was met (approval.approved == true)
    assert result.steps["fix"].output is not None

    # Verify ran because condition was met (fix.status == completed)
    assert result.steps["verification"].output is not None


@pytest.mark.asyncio
async def test_diagnose_fix_events_emitted():
    """All expected events are emitted during the 4-step workflow."""
    with open(WORKFLOW_YAML) as f:
        definition = yaml.safe_load(f)

    client = await Client.connect(TEMPORAL_URL)
    queue = f"e2e-{uuid.uuid4().hex[:8]}"
    wf_id = f"e2e-events-{uuid.uuid4().hex[:8]}"

    wf_input = WorkflowInput(
        definition=definition,
        workflow_id=wf_id,
        provider=ProviderConfig(name="openai", model="gpt-4", credentials_secret="test"),
        approval_policy={"auto_approve_risk_levels": ["low", "medium", "high", "critical"]},
    )

    async with Worker(
        client, task_queue=queue, workflows=[AgentWorkflow], activities=ALL_ACTIVITIES
    ):
        handle = await client.start_workflow(
            AgentWorkflow.run,
            wf_input,
            id=wf_id,
            task_queue=queue,
            execution_timeout=timedelta(seconds=120),
        )
        await handle.result()
        status = await handle.query(AgentWorkflow.get_status)

    event_types = [e.type for e in status.events]

    # diagnose step emits start + complete
    assert "step.started" in event_types
    assert "step.completed" in event_types

    # approval auto-approved
    assert "step.auto_approved" in event_types

    # All agent steps started
    started_steps = [e.step for e in status.events if e.type == "step.started"]
    assert "diagnose" in started_steps
    assert "fix" in started_steps
    assert "verify" in started_steps
