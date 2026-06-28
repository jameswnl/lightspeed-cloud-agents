"""Temporal activities for agent workflow execution.

Activities run in the worker process and handle I/O:
spawning sandbox pods, calling the LLM, building escalation packages.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC
from typing import Any, Optional

import httpx
from temporalio import activity

from agents.workflow.escalation import LogPackager
from agents.workflow.notifier import NullNotifier
from agents.workflow.temporal_context import build_sandbox_context
from agents.workflow.temporal_models import StepResult

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

    permissions = step.get("permissions") or {}
    if sa := permissions.get("service_account"):
        env_vars["LIGHTSPEED_SERVICE_ACCOUNT"] = sa
    http_timeout = float(permissions.get("timeout_seconds", 600))

    if spawner is None:
        logger.info("No spawner configured — returning stub result for '%s'", step_name)
        return {"status": "completed", "output": {"summary": f"executed-{step_name}"}}

    logger.info("Running sandbox step '%s' (pod=%s)", step_name, pod_name)
    endpoint = None
    try:
        endpoint = await spawner.spawn(
            pod_name, sandbox_image, env=env_vars, labels=labels,
        )
        ready = await spawner.wait_ready(endpoint)
        if not ready:
            raise RuntimeError(
                f"Sandbox pod '{pod_name}' never became ready for step '{step_name}'",
            )

        prior_steps = {
            k: StepResult(status=v.get("status", "completed"), output=v.get("output"), error=v.get("error"))
            for k, v in input.get("context", {}).items()
        }
        context = build_sandbox_context(
            workflow_steps=prior_steps,
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

        async with httpx.AsyncClient(timeout=http_timeout) as client:
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
async def send_approval_notification(input: dict[str, Any]) -> dict[str, Any]:
    """Send a notification when a workflow pauses for approval.

    Best-effort with possible duplicates. Single attempt, no retry.
    """
    workflow_id = input["workflow_id"]
    step_name = input["step_name"]
    message = input.get("message", "")
    correlation_id = f"{workflow_id}:{step_name}"

    try:
        notifier = NullNotifier()
        approve_url = f"/v1/workflows/{workflow_id}/approve"
        await notifier.notify(
            workflow_id=workflow_id,
            step_name=step_name,
            message=f"[{correlation_id}] {message}",
            approve_url=approve_url,
        )
        return {"status": "notification_sent", "correlation_id": correlation_id}
    except Exception:
        logger.warning(
            "Notification failed for %s (best-effort)", correlation_id, exc_info=True,
        )
        return {"status": "notification_failed", "correlation_id": correlation_id}


@activity.defn
async def build_escalation_activity(steps: dict[str, Any]) -> dict[str, Any]:
    """Package workflow context for escalation handoff.

    Primary artifact is always returned in the result (queryable via
    workflow status). External delivery via packager is secondary/best-effort.
    """
    failed_steps = [
        {"step": k, "error": v.get("error", "unknown")}
        for k, v in steps.items()
        if v.get("status") == "failed"
    ]

    result = {
        "status": "escalated",
        "output": {
            "type": "escalation_handoff",
            "failed_steps": failed_steps,
            "total_steps": len(steps),
        },
    }

    try:
        packager = LogPackager()
        from datetime import datetime

        from agents.workflow.escalation import EscalationPackage
        pkg = EscalationPackage(
            workflow_name="workflow",
            step_name=failed_steps[0]["step"] if failed_steps else "unknown",
            timestamp=datetime.now(tz=UTC).isoformat(),
            escalation=result["output"],
            workflow_snapshot=steps,
        )
        await packager.package(pkg)
    except Exception:
        logger.warning("Escalation delivery failed (best-effort)", exc_info=True)

    return result
