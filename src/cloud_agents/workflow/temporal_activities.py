"""Temporal activities for agent workflow execution.

Activities run in the worker process and handle I/O:
spawning sandbox pods, calling the LLM, building escalation packages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC
from typing import Any, Optional

import httpx
from temporalio import activity

from cloud_agents.runtime.tracing import get_tracer
from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.circuit_breaker import ProviderCircuitBreaker
from cloud_agents.workflow.escalation import LogPackager
from cloud_agents.workflow.notifier import NullNotifier
from cloud_agents.workflow.temporal_context import build_sandbox_context
from cloud_agents.workflow.temporal_models import StepResult

_tracer = get_tracer("cloud_agents.workflow.temporal_activities")

_circuit_breaker = ProviderCircuitBreaker(
    failure_threshold=int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "5")),
    reset_seconds=float(os.environ.get("CIRCUIT_BREAKER_RESET_SECONDS", "60")),
)

logger = logging.getLogger(__name__)


def _normalize_config_ref(ref: str) -> str:
    """Normalize a config ref to a valid env var segment.

    Replaces hyphens and other non-alphanumeric chars with underscores.
    e.g. 'slack-approval-channel' -> 'SLACK_APPROVAL_CHANNEL'
    """
    import re

    return re.sub(r"[^a-zA-Z0-9]", "_", ref).upper()


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
    """Spawn a sandbox pod, call POST /v1/agent/run, return result."""
    step = input["step"]
    step_name = step["name"]
    workflow_id = input["workflow_id"]
    with _tracer.start_as_current_span(
        "sandbox.step",
        attributes={"step.name": step_name, "workflow.id": workflow_id},
    ):
        return await _run_sandbox_step_inner(input, spawner)


async def _run_sandbox_step_inner(
    input: dict[str, Any],
    spawner: Optional[Any] = None,
) -> dict[str, Any]:
    """Inner implementation of run_sandbox_step."""
    step = input["step"]
    step_name = step["name"]
    workflow_id = input["workflow_id"]
    provider = input["provider"]
    sandbox_image = input.get("sandbox_image", "sandbox:latest")
    attempt = activity.info().attempt if activity.in_activity() else 1

    provider_name = provider.get("name", "unknown")
    if _circuit_breaker.is_open(provider_name):
        return {
            "status": "failed",
            "error": (
                f"Circuit breaker open for provider '{provider_name}' "
                "— too many consecutive failures"
            ),
        }

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
    if model_provider := provider.get("model_provider"):
        env_vars["LIGHTSPEED_MODEL_PROVIDER"] = model_provider
    elif val := os.environ.get("LIGHTSPEED_MODEL_PROVIDER"):
        env_vars["LIGHTSPEED_MODEL_PROVIDER"] = val
    for deploy_var in (
        "LIGHTSPEED_PROVIDER_URL",
        "LIGHTSPEED_PROVIDER_PROJECT",
        "LIGHTSPEED_PROVIDER_REGION",
        "LIGHTSPEED_PROVIDER_API_VERSION",
    ):
        if val := os.environ.get(deploy_var):
            env_vars[deploy_var] = val

    cred_secret = provider.get("credentials_secret", "")
    if cred_secret and (cred_val := os.environ.get(cred_secret)):
        env_vars[cred_secret] = cred_val

    # MCP server injection
    mcp_secret_mounts: list[tuple[str, str, str]] = []
    raw_mcp_servers = input.get("mcp_servers")
    if raw_mcp_servers:
        mcp_env_list = []
        for server in raw_mcp_servers:
            entry: dict[str, Any] = {
                "name": server["name"],
                "url": server["url"],
                "headers": dict(server.get("headers") or {}),
            }
            secret_headers = server.get("secret_headers") or {}
            for header_name, ref in secret_headers.items():
                mount_path = f"/var/secrets/mcp/{server['name']}/"
                file_path = f"/var/secrets/mcp/{server['name']}/{ref['key']}"
                entry["headers"][header_name] = {"file": file_path}
                mcp_secret_mounts.append((ref["secret_name"], ref["key"], mount_path))
                emit_audit(
                    event_type="mcp_secret_mounted",
                    workflow_id=workflow_id,
                    step_name=step_name,
                    details={
                        "secret_name": ref["secret_name"],
                        "server": server["name"],
                    },
                )
            mcp_env_list.append(entry)

        # Validate MCP secrets against allowlist
        allowed_secrets_raw = os.environ.get("MCP_ALLOWED_SECRETS", "")
        if allowed_secrets_raw:
            allowed = set(s.strip() for s in allowed_secrets_raw.split(","))
            for mount in mcp_secret_mounts:
                if mount[0] not in allowed:
                    raise ValueError(
                        f"MCP Secret '{mount[0]}' not in MCP_ALLOWED_SECRETS allowlist"
                    )

        env_vars["LIGHTSPEED_MCP_SERVERS"] = json.dumps(mcp_env_list)

    permissions = step.get("permissions") or {}
    if sa := permissions.get("service_account"):
        env_vars["LIGHTSPEED_SERVICE_ACCOUNT"] = sa
    http_timeout = float(permissions.get("timeout_seconds", 600))

    if spawner is None:
        logger.info("No spawner configured — returning stub result for '%s'", step_name)
        return {"status": "completed", "output": {"summary": f"executed-{step_name}"}}

    logger.info("Running sandbox step '%s' (pod=%s)", step_name, pod_name)
    emit_audit(
        event_type="sandbox_spawned",
        workflow_id=workflow_id,
        step_name=step_name,
        details={"pod_name": pod_name, "image": sandbox_image},
    )
    endpoint = None
    try:
        sa = permissions.get("service_account")
        advisory = step.get("advisory", False)
        if advisory and not sa:
            sa = "advisory-sa"

        endpoint = await spawner.spawn(
            pod_name,
            sandbox_image,
            env=env_vars,
            labels=labels,
            skills_image=input.get("skills_image"),
            skills_paths=input.get("skills_paths"),
            service_account=sa,
            read_only=advisory,
            credential_secret_name=provider.get("credentials_secret") or None,
            mcp_secret_mounts=mcp_secret_mounts or None,
        )
        ready = await spawner.wait_ready(endpoint, health_path="/health")
        if not ready:
            _circuit_breaker.record_failure(provider_name)
            raise RuntimeError(
                f"Sandbox pod '{pod_name}' never became ready for step '{step_name}'",
            )

        prior_steps = {
            k: StepResult(
                status=v.get("status", "completed"),
                output=v.get("output"),
                error=v.get("error"),
            )
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
        if permissions.get("allowed_tools"):
            request_body["allowedTools"] = permissions["allowed_tools"]
        if permissions.get("denied_tools"):
            request_body["deniedTools"] = permissions["denied_tools"]

        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.post(
                f"{endpoint}/v1/agent/run",
                json=request_body,
            )

        if response.status_code == 502:
            _circuit_breaker.record_failure(provider_name)
            raise RuntimeError(
                f"Infrastructure error from sandbox (HTTP 502) for step '{step_name}'",
            )

        data = response.json()

        if not data.get("success", False):
            _circuit_breaker.record_failure(provider_name)
            return {
                "status": "failed",
                "error": data.get("error", "agent returned success=false"),
                "output": data.get("output"),
            }

        _circuit_breaker.record_success(provider_name)
        output = data.get("output", {})
        for k, v in data.items():
            if k not in ("success", "output", "summary"):
                output[k] = v
        if summary := data.get("summary"):
            output["summary"] = summary
        return {
            "status": "completed",
            "output": output,
        }

    finally:
        if endpoint and spawner:
            if os.environ.get("SKIP_SANDBOX_DESTROY", "").lower() in ("1", "true"):
                logger.info(
                    "SKIP_SANDBOX_DESTROY set — keeping sandbox '%s' for inspection",
                    pod_name,
                )
                return result  # noqa: B012
            try:
                await spawner.destroy(pod_name)
                emit_audit(
                    event_type="sandbox_destroyed",
                    workflow_id=workflow_id,
                    step_name=step_name,
                    details={"pod_name": pod_name},
                )
            except Exception:
                logger.warning("Failed to destroy pod '%s'", pod_name, exc_info=True)
                from cloud_agents.workflow.temporal_metrics import ls_sandbox_cleanup_failures_total
                ls_sandbox_cleanup_failures_total.labels(step_name=step_name).inc()


@activity.defn
async def send_approval_notification(input: dict[str, Any]) -> dict[str, Any]:
    """Send a notification when a workflow pauses for approval."""
    workflow_id = input["workflow_id"]
    step_name = input["step_name"]
    with _tracer.start_as_current_span(
        "notification.send",
        attributes={"workflow.id": workflow_id, "step.name": step_name},
    ):
        return await _send_notification_inner(input)


async def _send_notification_inner(input: dict[str, Any]) -> dict[str, Any]:
    """Inner implementation of send_approval_notification."""
    workflow_id = input["workflow_id"]
    step_name = input["step_name"]
    message = input.get("message", "")
    correlation_id = f"{workflow_id}:{step_name}"

    try:
        config = input.get("notifier_config") or {}
        notifier_type = config.get("type", "null")
        if notifier_type == "slack":
            from cloud_agents.workflow.notifier import SlackNotifier

            ref = _normalize_config_ref(config.get("config_ref", "DEFAULT"))
            webhook_url = os.environ.get(f"NOTIFIER_SLACK_{ref}_WEBHOOK_URL", "")
            notifier = SlackNotifier(webhook_url=webhook_url)
        elif notifier_type == "webhook":
            from cloud_agents.workflow.notifier import WebhookNotifier

            ref = _normalize_config_ref(config.get("config_ref", "DEFAULT"))
            url = os.environ.get(f"NOTIFIER_WEBHOOK_{ref}_URL", "")
            notifier = WebhookNotifier(url=url)
        else:
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
            "Notification failed for %s (best-effort)",
            correlation_id,
            exc_info=True,
        )
        return {"status": "notification_failed", "correlation_id": correlation_id}


@activity.defn
async def build_escalation_activity(
    steps: dict[str, Any],
    workflow_name: str = "workflow",
    escalation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Package workflow context for escalation handoff."""
    with _tracer.start_as_current_span(
        "escalation.build",
        attributes={"workflow.name": workflow_name},
    ):
        return await _build_escalation_inner(steps, workflow_name, escalation_config)


async def _build_escalation_inner(
    steps: dict[str, Any],
    workflow_name: str = "workflow",
    escalation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inner implementation of build_escalation_activity."""
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
        config_type = (escalation_config or {}).get("type", "log")
        if config_type == "webhook":
            from cloud_agents.workflow.escalation import WebhookPackager

            ref = _normalize_config_ref(
                (escalation_config or {}).get("config_ref", "DEFAULT")
            )
            url = os.environ.get(f"ESCALATION_WEBHOOK_{ref}_URL", "")
            packager = WebhookPackager(url=url)
        else:
            packager = LogPackager()

        from datetime import datetime

        from cloud_agents.workflow.escalation import EscalationPackage

        pkg = EscalationPackage(
            workflow_name=workflow_name,
            step_name=failed_steps[0]["step"] if failed_steps else "unknown",
            timestamp=datetime.now(tz=UTC).isoformat(),
            escalation=result["output"],
            workflow_snapshot=steps,
        )
        await packager.package(pkg)
        emit_audit(
            event_type="escalation_triggered",
            workflow_id=workflow_name,
            details={"failed_steps": failed_steps, "delivery": config_type},
        )
    except Exception:
        logger.warning("Escalation delivery failed (best-effort)", exc_info=True)

    return result
