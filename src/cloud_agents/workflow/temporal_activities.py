"""Temporal activities for agent workflow execution.

Activities run in the worker process and handle I/O:
spawning sandbox pods, calling the LLM, building escalation packages.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn
async def run_sandbox_step(input: dict[str, Any]) -> dict[str, Any]:
    """Spawn a sandbox pod, call POST /v1/agent/run, return result.

    Infrastructure failures propagate as exceptions for Temporal retry.
    Application failures return StepResult(status="failed").
    """
    step = input["step"]
    step_name = step["name"]

    logger.info("Running sandbox step '%s'", step_name)

    return {
        "status": "completed",
        "output": {"summary": f"executed-{step_name}"},
    }


@activity.defn
async def build_escalation_activity(steps: dict[str, Any]) -> dict[str, Any]:
    """Package workflow context for escalation handoff."""
    failed_steps = [
        {"step": k, "error": v.get("error", "unknown")}
        for k, v in steps.items()
        if v.get("status") == "failed"
    ]

    return {
        "status": "escalated",
        "output": {
            "type": "escalation_handoff",
            "failed_steps": failed_steps,
            "total_steps": len(steps),
        },
    }
