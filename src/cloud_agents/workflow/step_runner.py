"""Temporal-free step runner for sandbox agent execution.

Extracted from temporal_activities.py. Contains the core logic for:
- Spawning a sandbox container
- Calling POST /v1/agent/run
- Collecting transcripts
- Destroying the sandbox

No temporalio imports. Both Temporal and local executors call this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from typing import Any, Optional

import httpx

from cloud_agents.runtime.auth import get_runner_auth_token
from cloud_agents.runtime.tracing import get_tracer
from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.circuit_breaker import ProviderCircuitBreaker
from cloud_agents.workflow.redact import redact_secrets
from cloud_agents.workflow.temporal_context import build_sandbox_context
from cloud_agents.workflow.temporal_models import StepResult, StepTranscript, TranscriptEvent
from cloud_agents.workflow.temporal_metrics import (
    ls_sandbox_cleanup_failures_total,
    ls_sandbox_tls_errors_total,
    ls_sandbox_timeout_total,
)
from cloud_agents.workflow.tls import TLSMode, generate_ephemeral_certs, get_tls_mode

_tracer = get_tracer("cloud_agents.workflow.step_runner")

_EVENT_LOG_PATH = "/tmp/agent-events.jsonl"

_circuit_breaker = ProviderCircuitBreaker(
    failure_threshold=int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "5")),
    reset_seconds=float(os.environ.get("CIRCUIT_BREAKER_RESET_SECONDS", "60")),
)

logger = logging.getLogger(__name__)


def _to_k8s_secret_name(name: str | None) -> str | None:
    """Convert a credentials_secret value to a valid K8s Secret name."""
    if not name:
        return None
    return name.lower().replace("_", "-")


def compute_pod_name(workflow_id: str, step_name: str, attempt: int) -> str:
    """Compute a content-hash pod name for idempotent spawning."""
    import hashlib

    hash_input = f"{workflow_id}:{step_name}:{attempt}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"ca-{digest}"


async def _collect_transcript(
    endpoint: str,
    step_name: str,
    client_kwargs: dict[str, Any],
    http_headers: dict[str, str],
) -> StepTranscript:
    """Collect the agent event transcript via HTTP from the sandbox."""
    empty = StepTranscript(step_name=step_name)

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(
                f"{endpoint}/v1/agent/events",
                headers=http_headers or None,
            )

        if response.status_code == 404:
            return empty

        response.raise_for_status()
        content = response.text
    except Exception:
        logger.warning(
            "Failed to collect transcript for step '%s'", step_name, exc_info=True
        )
        return empty

    if not content or not content.strip():
        return empty

    events: list[TranscriptEvent] = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            events.append(
                TranscriptEvent(
                    ts=raw.get("ts", ""),
                    type=raw.get("type", "result"),
                    data=raw.get("data", {}),
                )
            )
        except (json.JSONDecodeError, Exception):
            continue

    return StepTranscript(step_name=step_name, events=events)


async def run_step(
    input: dict[str, Any],
    *,
    spawner: Optional[Any] = None,
    transcript_store: Optional[Any] = None,
    attempt: int = 1,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Run a single agent step in a sandbox container.

    Parameters:
        input: Step input including step spec, workflow_id, provider, sandbox_image.
        spawner: AgentSpawner instance for sandbox lifecycle.
        transcript_store: Optional store for persisting agent transcripts.
        attempt: Retry attempt number (affects pod name for idempotency).
        on_progress: Optional async callback for progress events.

    Returns:
        Dict with status, output, and optional transcript.
    """
    step = input["step"]
    step_name = step["name"]
    workflow_id = input["workflow_id"]

    with _tracer.start_as_current_span(
        "sandbox.step",
        attributes={"step.name": step_name, "workflow.id": workflow_id},
    ):
        return await _run_step_inner(
            input,
            spawner=spawner,
            transcript_store=transcript_store,
            attempt=attempt,
            on_progress=on_progress,
        )


async def _run_step_inner(
    input: dict[str, Any],
    *,
    spawner: Optional[Any] = None,
    transcript_store: Optional[Any] = None,
    attempt: int = 1,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Inner implementation — no tracing wrapper."""
    step = input["step"]
    step_name = step["name"]
    workflow_id = input["workflow_id"]
    provider = input["provider"]
    sandbox_image = input.get("sandbox_image", "sandbox:latest")

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
        "AGENT_EVENT_LOG": _EVENT_LOG_PATH,
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

    secret_values: set[str] = set()

    cred_secret = provider.get("credentials_secret", "")
    if cred_secret:
        env_key = cred_secret.upper().replace("-", "_")
        cred_val = os.environ.get(env_key) or os.environ.get(cred_secret)
        if cred_val:
            env_vars[env_key] = cred_val
            secret_values.add(cred_val)

    # MCP server injection
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
    sandbox_auth_enabled = os.environ.get("SANDBOX_AUTH_ENABLED", "false").lower() == "true"
    sandbox_auth_token: str | None = None
    if sandbox_auth_enabled:
        sandbox_auth_token = get_runner_auth_token()
        if sandbox_auth_token:
            env_vars["AGENT_API_TOKEN"] = sandbox_auth_token
        else:
            logger.warning(
                "SANDBOX_AUTH_ENABLED=true but no auth token available — "
                "sandbox will run unauthenticated."
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
                credential_secret_name=_to_k8s_secret_name(
                    provider.get("credentials_secret")
                )
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

            client_kwargs: dict[str, Any] = {"timeout": http_timeout}
            if tls_mode == TLSMode.APP and tls_certs:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.load_verify_locations(cadata=tls_certs.ca_cert_pem.decode())
                client_kwargs["verify"] = ssl_ctx

            http_headers: dict[str, str] = {}
            if sandbox_auth_enabled and sandbox_auth_token:
                http_headers["Authorization"] = f"Bearer {sandbox_auth_token}"

            # OpenShell-specific: merge gateway routing headers
            progress_task: asyncio.Task | None = None
            from cloud_agents.spawner.openshell_spawner import OpenShellSpawner

            if isinstance(spawner, OpenShellSpawner):
                http_headers.update(spawner.get_sandbox_headers(pod_name))
                sandbox_id = spawner.get_sandbox_id(pod_name)
                if sandbox_id and on_progress:
                    progress_task = asyncio.create_task(
                        _stream_progress(spawner, sandbox_id, on_progress)
                    )

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

            transcript = await _collect_transcript(
                endpoint, step_name, client_kwargs, http_headers
            )

            output_key = step.get("output_key", step_name)
            if transcript_store is not None and transcript.events:
                try:
                    await transcript_store.save(workflow_id, output_key, transcript)
                except Exception:
                    logger.warning(
                        "Failed to save transcript to store for step '%s'",
                        output_key,
                        exc_info=True,
                    )

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
            output = data.get("output") or {}
            if not isinstance(output, dict):
                output = {"raw": output}
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
                    logger.warning(
                        "Failed to destroy pod '%s'", pod_name, exc_info=True
                    )
                    ls_sandbox_cleanup_failures_total.labels(
                        step_name=step_name
                    ).inc()


async def _stream_progress(
    spawner: Any,
    sandbox_id: str,
    on_progress: Any,
) -> None:
    """Stream progress events from a spawner and forward to callback."""
    try:
        async for event in spawner.stream_progress(sandbox_id):
            try:
                await on_progress(event)
            except Exception:
                logger.debug("Progress callback failed (best-effort)", exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Progress streaming error for sandbox '%s'", sandbox_id, exc_info=True)
