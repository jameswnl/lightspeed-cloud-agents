"""Parallel step grouping and validation for workflow execution.

Groups consecutive steps with the same parallel_group for concurrent
execution via asyncio.gather().
"""

from __future__ import annotations

import logging
from typing import Any

from agents.workflow.definition import WorkflowStepSpec

logger = logging.getLogger(__name__)

StepBatch = list[WorkflowStepSpec]


def group_steps(steps: list[WorkflowStepSpec]) -> list[StepBatch]:
    """Group steps into sequential and parallel batches.

    Consecutive steps with the same parallel_group are batched together.
    Steps without parallel_group are individual batches (sequential).

    Args:
        steps: Ordered list of workflow steps.

    Returns:
        List of batches. Single-step batches run sequentially,
        multi-step batches run concurrently.
    """
    if not steps:
        return []

    batches: list[StepBatch] = []
    current_batch: StepBatch = [steps[0]]
    current_group = steps[0].parallel_group

    for step in steps[1:]:
        if step.parallel_group and step.parallel_group == current_group:
            current_batch.append(step)
        else:
            batches.append(current_batch)
            current_batch = [step]
            current_group = step.parallel_group

    batches.append(current_batch)
    return batches


def validate_parallel_groups(steps: list[WorkflowStepSpec]) -> list[str]:
    """Validate parallel group constraints.

    Rules:
    - Approval steps cannot be in parallel groups
    - Warns (but does not block) if two steps in a group target the same agent

    Args:
        steps: Ordered list of workflow steps.

    Returns:
        List of validation error messages (empty if valid).
    """
    errors: list[str] = []
    groups: dict[str, list[WorkflowStepSpec]] = {}

    for step in steps:
        if step.parallel_group:
            if step.type == "human-approval":
                errors.append(
                    f"Step '{step.name}': human-approval steps cannot be in parallel groups"
                )
            groups.setdefault(step.parallel_group, []).append(step)

    for group_name, group_steps in groups.items():
        agents = [s.agent for s in group_steps if s.agent]
        if len(agents) != len(set(agents)):
            logger.warning(
                "Parallel group '%s': multiple steps target the same agent. "
                "Ensure they are side-effect-safe.",
                group_name,
            )

    return errors
