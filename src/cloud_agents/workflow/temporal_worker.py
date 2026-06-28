"""Temporal worker configuration and startup.

Registers the AgentWorkflow and activities, configures task queue
and concurrency settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agents.workflow.temporal_activities import (
    build_escalation_activity,
    run_sandbox_step,
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


def build_worker_config(
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_concurrent_activities: int = DEFAULT_MAX_CONCURRENT_ACTIVITIES,
) -> WorkerConfig:
    """Build worker configuration with registered workflows and activities.

    Parameters:
        task_queue: Temporal task queue name.
        max_concurrent_activities: Max activities running concurrently.

    Returns:
        WorkerConfig with registered workflows and activities.
    """
    return WorkerConfig(
        task_queue=task_queue,
        max_concurrent_activities=max_concurrent_activities,
        workflows=[AgentWorkflow],
        activities=[run_sandbox_step, build_escalation_activity],
    )
