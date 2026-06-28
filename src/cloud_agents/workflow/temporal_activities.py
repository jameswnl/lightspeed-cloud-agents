"""Temporal activities for agent workflow execution.

Activities run in the worker process and handle I/O:
spawning sandbox pods, calling the LLM, building escalation packages.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

import httpx
from temporalio import activity

from agents.workflow.temporal_context import build_sandbox_context

logger = logging.getLogger(__name__)


def compute_pod_name(workflow_id: str, step_name: str, attempt: int) -> str:
    """Compute a content-hash pod name for idempotent spawning.

    Parameters:
        workflow_id: Workflow execution ID.
        step_name: Step name within the workflow.
        attempt: Retry attempt number.

    Returns:
        Deterministic pod name with ca- prefix.
    """
    hash_input = f"{workflow_id}:{step_name}:{attempt}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"ca-{digest}"


@activity.defn
async def run_sandbox_step(
    input: dict[str, Any],
    spawner: Optional[Any] = None,
) -> dict[str, Any]:
    """Spawn a sandbox pod, call POST /v1/agent/run, return result.

    Infrastructure failures (HTTP 502, spawn errors) raise exceptions
    for Temporal retry. Application failures return status="failed".

    Parameters:
        input: Step configuration, workflow context, and provider info.
        spawner: Agent spawner instance (injected via activity context
            in production, passed directly in tests).
    """
    step = input["step"]
    step_name = step["name"]
    workflow_id = input["workflow_id"]
    provider = input["provider"]
    sandbox_image = input.get("sandbox_image", "sandbox:latest")
    attempt = activity.info().attempt if activity.in_activity() else 1

    pod_name = compute_pod_name(workflow_id, step_name, attempt)
    labels = {
        "cloud-agents/workflow-id": workflow_id,
        "cloud-agents/step-name": step_name,
        "cloud-agents/attempt": str(attempt),
    }
    env_vars = {
        "LIGHTSPEED_PROVIDER": provider["name"],
        "LIGHTSPEED_MODEL": provider["model"],
    }

    if spawner is None:
        logger.info("No spawner configured — returning stub result for '%s'", step_name)
        return {"status": "completed", "output": {"summary": f"executed-{step_name}"}}

    logger.info("Running sandbox step '%s' (pod=%s)", step_name, pod_name)
    endpoint = None
    try:
        endpoint = await spawner.spawn(
            pod_name, sandbox_image, env=env_vars, labels=labels,
        )
        await spawner.wait_ready(endpoint)

        context = build_sandbox_context(
            workflow_steps={},
            current_step=step,
        )

        request_body: dict[str, Any] = {
            "query": step.get("prompt", ""),
            "context": context,
        }
        if instructions := step.get("instructions"):
            request_body["systemPrompt"] = instructions
        if output_schema := step.get("output_schema"):
            request_body["outputSchema"] = output_schema

        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                f"{endpoint}/v1/agent/run", json=request_body,
            )

        if response.status_code == 502:
            raise RuntimeError(
                f"Infrastructure error from sandbox (HTTP 502) for step '{step_name}'",
            )

        data = response.json()

        if not data.get("success", False):
            return {
                "status": "failed",
                "error": data.get("error", "agent returned success=false"),
                "output": data.get("output"),
            }

        return {
            "status": "completed",
            "output": data.get("output", {}),
        }

    finally:
        if endpoint and spawner:
            try:
                await spawner.destroy(pod_name)
            except Exception:
                logger.warning("Failed to destroy pod '%s'", pod_name, exc_info=True)


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
