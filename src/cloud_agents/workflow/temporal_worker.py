"""Temporal worker configuration and startup.

Registers the AgentWorkflow and activities, configures task queue
and concurrency settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from temporalio import activity

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
    send_approval_notification,
)
from agents.workflow.temporal_workflow import AgentWorkflow

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


def _bind_sandbox_activity(spawner: Any):
    """Create a bound sandbox activity with the spawner injected."""
    @activity.defn(name="run_sandbox_step")
    async def bound_run_sandbox_step(input: dict[str, Any]) -> dict[str, Any]:
        return await run_sandbox_step(input, spawner=spawner)
    return bound_run_sandbox_step


def build_worker_config(
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_concurrent_activities: int = DEFAULT_MAX_CONCURRENT_ACTIVITIES,
    spawner: Optional[Any] = None,
) -> WorkerConfig:
    """Build worker configuration with registered workflows and activities.

    Parameters:
        task_queue: Temporal task queue name.
        max_concurrent_activities: Max activities running concurrently.
        spawner: Agent spawner instance for sandbox activities.

    Returns:
        WorkerConfig with registered workflows and activities.
    """
    if spawner is not None:
        sandbox_activity = _bind_sandbox_activity(spawner)
    else:
        sandbox_activity = run_sandbox_step

    return WorkerConfig(
        task_queue=task_queue,
        max_concurrent_activities=max_concurrent_activities,
        workflows=[AgentWorkflow],
        activities=[sandbox_activity, build_escalation_activity, send_approval_notification],
    )
