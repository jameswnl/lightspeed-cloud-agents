"""Temporal worker configuration and startup.

Registers the AgentWorkflow and activities, configures task queue
and concurrency settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from temporalio import activity

from cloud_agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
    send_approval_notification,
)
from cloud_agents.workflow.temporal_workflow import AgentWorkflow

logger = logging.getLogger(__name__)

DEFAULT_TASK_QUEUE = "cloud-agents"
DEFAULT_MAX_CONCURRENT_ACTIVITIES = 10


@dataclass
class WorkerConfig:
    """Configuration for a Temporal worker."""

    task_queue: str = DEFAULT_TASK_QUEUE
    max_concurrent_activities: int = DEFAULT_MAX_CONCURRENT_ACTIVITIES
    workflows: list[type] = field(default_factory=list)
    activities: list[Any] = field(default_factory=list)


def _bind_sandbox_activity(spawner: Any, transcript_store: Any = None):
    """Create a bound sandbox activity with the spawner and store injected."""

    @activity.defn(name="run_sandbox_step")
    async def bound_run_sandbox_step(input: dict[str, Any]) -> dict[str, Any]:
        return await run_sandbox_step(
            input, spawner=spawner, transcript_store=transcript_store
        )

    return bound_run_sandbox_step


def _bind_escalation_activity(transcript_store: Any = None):
    """Create a bound escalation activity with the store injected."""

    @activity.defn(name="build_escalation_activity")
    async def bound_build_escalation(
        steps: dict[str, Any],
        workflow_name: str = "workflow",
        escalation_config: dict[str, Any] | None = None,
        definition: dict[str, Any] | None = None,
        input_prompt: str | None = None,
        events: list[dict[str, Any]] | None = None,
        provider_name: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        return await build_escalation_activity(
            steps,
            workflow_name,
            escalation_config,
            definition=definition,
            input_prompt=input_prompt,
            events=events,
            provider_name=provider_name,
            workflow_id=workflow_id,
            transcript_store=transcript_store,
        )

    return bound_build_escalation


def build_worker_config(
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_concurrent_activities: int = DEFAULT_MAX_CONCURRENT_ACTIVITIES,
    spawner: Optional[Any] = None,
    transcript_store: Optional[Any] = None,
) -> WorkerConfig:
    """Build worker configuration with registered workflows and activities.

    Parameters:
        task_queue: Temporal task queue name.
        max_concurrent_activities: Max activities running concurrently.
        spawner: Agent spawner instance for sandbox activities.
        transcript_store: Optional TranscriptStore for full transcript
            persistence in PostgreSQL.

    Returns:
        WorkerConfig with registered workflows and activities.
    """
    if spawner is not None:
        sandbox_activity = _bind_sandbox_activity(spawner, transcript_store)
    else:
        sandbox_activity = run_sandbox_step

    if transcript_store is not None:
        escalation_activity = _bind_escalation_activity(transcript_store)
    else:
        escalation_activity = build_escalation_activity

    return WorkerConfig(
        task_queue=task_queue,
        max_concurrent_activities=max_concurrent_activities,
        workflows=[AgentWorkflow],
        activities=[
            sandbox_activity,
            escalation_activity,
            send_approval_notification,
        ],
    )
