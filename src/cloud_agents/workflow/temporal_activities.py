"""Temporal activities for agent workflow execution.

Activities run in the worker process and handle I/O:
spawning sandbox pods, calling the LLM, building escalation packages.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import ssl
from datetime import UTC
from typing import Any, Optional

import httpx
from temporalio import activity

from cloud_agents.runtime.auth import get_runner_auth_token
from cloud_agents.runtime.tracing import get_tracer
from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.circuit_breaker import ProviderCircuitBreaker
from cloud_agents.workflow.escalation import LogPackager
from cloud_agents.workflow.notifier import NullNotifier
from cloud_agents.workflow.redact import redact_secrets
from cloud_agents.workflow.temporal_context import build_sandbox_context
from cloud_agents.workflow.temporal_metrics import ls_sandbox_tls_errors_total
from cloud_agents.workflow.temporal_models import StepResult, StepTranscript, TranscriptEvent
from cloud_agents.workflow.tls import TLSMode, generate_ephemeral_certs, get_tls_mode

_tracer = get_tracer("cloud_agents.workflow.temporal_activities")

# Maximum heartbeat payload size in bytes for progress events.
# Temporal has payload limits; we truncate to a summary.
_MAX_HEARTBEAT_BYTES = 1024

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


def _to_k8s_secret_name(name: str | None) -> str | None:
    """Convert a credentials_secret value to a valid K8s Secret name.

    e.g. 'OPENAI_API_KEY' -> 'openai-api-key'
    """
    if not name:
        return None
    return name.lower().replace("_", "-")


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


async def _heartbeat_loop(interval_seconds: float = 30) -> None:
    """Send periodic heartbeats during sandbox HTTP call.

    Calls activity.heartbeat() in a loop so the Temporal server can
    detect stale workers.  Errors are logged but swallowed (best-effort).

    Parameters:
        interval_seconds: Seconds between heartbeat calls.
    """
    while True:
        try:
            activity.heartbeat()
        except Exception:
            logger.debug("Heartbeat failed (best-effort)", exc_info=True)
        await asyncio.sleep(interval_seconds)


def _truncate_heartbeat_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Truncate a progress event to a heartbeat-safe summary.

    Temporal heartbeat payloads have size limits. This function extracts
    only the event type and tool name, keeping the payload under 1KB.

    Parameters:
        event: Raw JSONL event from the agent event log.

    Returns:
        Truncated summary dict suitable for activity.heartbeat().
    """
    event_type = event.get("type", "unknown")
    summary: dict[str, Any] = {
        "event_type": event_type[:200],
    }
    if name := event.get("name"):
        summary["tool"] = name[:200]
    return summary


async def _progress_streaming_loop(
    spawner: Any,
    sandbox_id: str,
) -> None:
    """Stream progress events from an OpenShellSpawner and heartbeat them.

    Best-effort: errors are logged and the loop exits gracefully.
    The caller is responsible for cancelling this task when the HTTP
    result returns.

    Parameters:
        spawner: An OpenShellSpawner instance with stream_progress().
        sandbox_id: The sandbox to stream progress from.
    """
    # TODO: Add heartbeat debounce for production
    try:
        async for event in spawner.stream_progress(sandbox_id):
            payload = _truncate_heartbeat_payload(event)
            try:
                activity.heartbeat(payload)
            except Exception:
                logger.debug("Progress heartbeat failed (best-effort)", exc_info=True)
    except asyncio.CancelledError:
        logger.debug("Progress streaming cancelled for sandbox '%s'", sandbox_id)
        raise
    except Exception:
        logger.warning(
            "Progress streaming error for sandbox '%s'",
            sandbox_id,
            exc_info=True,
        )


# Path where the sandbox agent writes structured JSONL events
_EVENT_LOG_PATH = "/var/log/agent-events.jsonl"


async def _collect_transcript(
    spawner: Any,
    pod_name: str,
    step_name: str,
) -> StepTranscript:
    """Collect the agent event transcript from the sandbox container.

    Reads /var/log/agent-events.jsonl from the sandbox via spawner.read_file(),
    parses each line into a TranscriptEvent, and returns a StepTranscript.

    Graceful degradation: returns an empty transcript if the file doesn't
    exist (Task 5 not shipped yet) or if any error occurs during collection.

    Parameters:
        spawner: Agent spawner instance (must have read_file method).
        pod_name: Name of the sandbox pod/container.
        step_name: Name of the workflow step (for the transcript).

    Returns:
        StepTranscript with parsed events, or empty transcript on failure.
    """
    empty = StepTranscript(step_name=step_name)

    read_file = getattr(spawner, "read_file", None)
    if read_file is None or not callable(read_file):
        return empty

    try:
        content = await read_file(pod_name, _EVENT_LOG_PATH)
    except (FileNotFoundError, OSError):
        logger.debug("Event log not found for step '%s' (graceful degradation)", step_name)
        return empty
    except Exception:
        logger.warning("Failed to read event log for step '%s'", step_name, exc_info=True)
        return empty

    if not isinstance(content, str):
        return empty

    events: list[TranscriptEvent] = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            event = TranscriptEvent(
                ts=raw.get("ts", ""),
                type=raw.get("type", "result"),
                data=raw.get("data", {}),
            )
            events.append(event)
        except (json.JSONDecodeError, Exception):
            logger.debug("Skipping invalid JSONL line in event log: %s", line[:200])
            continue

    return StepTranscript(step_name=step_name, events=events)


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

    # Track secret values for redaction in error paths
    secret_values: set[str] = set()

    cred_secret = provider.get("credentials_secret", "")
    if cred_secret and (cred_val := os.environ.get(cred_secret)):
        env_vars[cred_secret] = cred_val
        secret_values.add(cred_val)

    # MCP server injection — step references servers by name from workflow-level catalog
    mcp_secret_mounts: list[tuple[str, str, str]] = []
    step_mcp_names = step.get("mcp_servers")
    all_mcp_servers = input.get("mcp_servers") or []
    if step_mcp_names:
        mcp_by_name = {s["name"]: s for s in all_mcp_servers}
        raw_mcp_servers = [mcp_by_name[n] for n in step_mcp_names if n in mcp_by_name]
    else:
        raw_mcp_servers = None
    if raw_mcp_servers:
        mcp_env_list = []
        for server in raw_mcp_servers:
            plain_headers = dict(server.get("headers") or {})
            # Track plain-text header values as secrets for redaction
            for header_val in plain_headers.values():
                if isinstance(header_val, str) and header_val:
                    secret_values.add(header_val)
            entry: dict[str, Any] = {
                "name": server["name"],
                "url": server["url"],
                "headers": plain_headers,
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

    # Runner-to-sandbox bearer token auth
    sandbox_auth_enabled = (
        os.environ.get("SANDBOX_AUTH_ENABLED", "false").lower() == "true"
    )
    sandbox_auth_token: str | None = None
    if sandbox_auth_enabled:
        sandbox_auth_token = get_runner_auth_token()
        if sandbox_auth_token:
            env_vars["AGENT_API_TOKEN"] = sandbox_auth_token
        else:
            logger.warning(
                "SANDBOX_AUTH_ENABLED=true but no auth token available — "
                "sandbox will run unauthenticated. Set AGENT_API_TOKEN or "
                "configure AUTH_MODE=sa_token with a projected volume."
            )

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
    # TLS cert generation for app-level encryption
    tls_mode = get_tls_mode()
    tls_certs = None
    if tls_mode == TLSMode.APP:
        namespace = os.environ.get("NAMESPACE", "default")
        san_dns = [
            pod_name,
            f"agent-{pod_name}",
            f"agent-{pod_name}.{namespace}.svc",
            f"agent-{pod_name}.{namespace}.svc.cluster.local",
            "localhost",
        ]
        tls_certs = generate_ephemeral_certs(
            common_name=pod_name,
            san_dns=san_dns,
            san_ips=["127.0.0.1"],
        )

    endpoint = None
    was_cancelled = False
    try:
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
                credential_secret_name=_to_k8s_secret_name(provider.get("credentials_secret"))
                or None,
                mcp_secret_mounts=mcp_secret_mounts or None,
                tls_certs=tls_certs,
            )
            ready = await spawner.wait_ready(
                endpoint,
                health_path="/health",
                ca_cert_pem=tls_certs.ca_cert_pem if tls_certs else None,
            )
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

            # Configure httpx client for TLS verification
            client_kwargs: dict[str, Any] = {"timeout": http_timeout}
            if tls_mode == TLSMode.APP and tls_certs:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.load_verify_locations(cadata=tls_certs.ca_cert_pem.decode())
                client_kwargs["verify"] = ssl_ctx

            # Build HTTP headers for sandbox call
            http_headers: dict[str, str] = {}
            if sandbox_auth_enabled and sandbox_auth_token:
                http_headers["Authorization"] = f"Bearer {sandbox_auth_token}"

            # Start progress streaming for OpenShell spawners (best-effort)
            progress_task: asyncio.Task | None = None
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            if isinstance(spawner, OpenShellSpawner):
                sandbox_id = spawner.get_sandbox_id(pod_name)
                if sandbox_id:
                    progress_task = asyncio.create_task(
                        _progress_streaming_loop(spawner, sandbox_id)
                    )

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    try:
                        response = await client.post(
                            f"{endpoint}/v1/agent/run",
                            json=request_body,
                            headers=http_headers or None,
                        )
                    except ssl.SSLError as tls_exc:
                        emit_audit(
                            event_type="tls_error",
                            workflow_id=workflow_id,
                            step_name=step_name,
                            details={
                                "pod_name": pod_name,
                                "error": str(tls_exc),
                            },
                        )
                        ls_sandbox_tls_errors_total.labels(
                            step_name=step_name,
                            error_type="ssl_error",
                        ).inc()
                        raise
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                if progress_task is not None:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task

            if response.status_code == 502:
                _circuit_breaker.record_failure(provider_name)
                raise RuntimeError(
                    f"Infrastructure error from sandbox (HTTP 502) for step '{step_name}'",
                )

            data = response.json()

            # Collect transcript from sandbox event log (before destroy)
            transcript = await _collect_transcript(spawner, pod_name, step_name)
            transcript_dict = transcript.model_dump()

            if not data.get("success", False):
                _circuit_breaker.record_failure(provider_name)
                error_msg = data.get("error", "agent returned success=false")
                output_val = data.get("output")
                if secret_values:
                    error_msg = redact_secrets(str(error_msg), secret_values)
                    if output_val:
                        output_val = json.loads(
                            redact_secrets(json.dumps(output_val), secret_values)
                        )
                return {
                    "status": "failed",
                    "error": error_msg,
                    "output": output_val,
                    "transcript": transcript_dict,
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
                "transcript": transcript_dict,
            }
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            if secret_values:
                redacted_msg = redact_secrets(str(exc), secret_values)
                raise RuntimeError(redacted_msg) from None
            raise

    finally:
        if was_cancelled and endpoint:
            from cloud_agents.workflow.temporal_metrics import ls_sandbox_timeout_total

            ls_sandbox_timeout_total.labels(
                step_name=step_name, reason="cancelled"
            ).inc()
            emit_audit(
                event_type="sandbox_timeout",
                workflow_id=workflow_id,
                step_name=step_name,
                details={"pod_name": pod_name, "reason": "cancelled"},
            )

        if endpoint and spawner:
            if os.environ.get("SKIP_SANDBOX_DESTROY", "").lower() in ("1", "true"):
                logger.info(
                    "SKIP_SANDBOX_DESTROY set — keeping sandbox '%s' for inspection",
                    pod_name,
                )
            else:
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
                    from cloud_agents.workflow.temporal_metrics import (
                        ls_sandbox_cleanup_failures_total,
                    )

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
    definition: dict[str, Any] | None = None,
    input_prompt: str | None = None,
    events: list[dict[str, Any]] | None = None,
    provider_name: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Package workflow context for escalation handoff."""
    with _tracer.start_as_current_span(
        "escalation.build",
        attributes={"workflow.name": workflow_name},
    ):
        return await _build_escalation_inner(
            steps,
            workflow_name,
            escalation_config,
            definition=definition,
            input_prompt=input_prompt,
            events=events,
            provider_name=provider_name,
            workflow_id=workflow_id,
        )


async def _build_escalation_inner(
    steps: dict[str, Any],
    workflow_name: str = "workflow",
    escalation_config: dict[str, Any] | None = None,
    definition: dict[str, Any] | None = None,
    input_prompt: str | None = None,
    events: list[dict[str, Any]] | None = None,
    provider_name: str | None = None,
    workflow_id: str | None = None,
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

            ref = _normalize_config_ref((escalation_config or {}).get("config_ref", "DEFAULT"))
            url = os.environ.get(f"ESCALATION_WEBHOOK_{ref}_URL", "")
            packager = WebhookPackager(url=url)
        elif config_type == "jira":
            from cloud_agents.workflow.escalation import JiraPackager

            ref = _normalize_config_ref((escalation_config or {}).get("config_ref", "DEFAULT"))
            url = os.environ.get(f"ESCALATION_JIRA_{ref}_URL", "")
            project_key = os.environ.get(f"ESCALATION_JIRA_{ref}_PROJECT_KEY", "")
            packager = JiraPackager(url=url, project_key=project_key)
        elif config_type == "cli_handoff":
            from cloud_agents.workflow.escalation import CLIHandoffPackager

            ref = _normalize_config_ref((escalation_config or {}).get("config_ref", "DEFAULT"))
            output_dir = os.environ.get(
                f"ESCALATION_CLI_HANDOFF_{ref}_DIR",
                "/tmp/cloud-agents-handoff",
            )
            packager = CLIHandoffPackager(output_dir=output_dir)
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
            definition=definition,
            input_prompt=input_prompt,
            events=events,
            provider_name=provider_name,
            workflow_id=workflow_id,
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
