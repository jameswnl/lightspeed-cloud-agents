"""Build the stdin request for the sandbox step CLI.

The step CLI (lightspeed-agentic-sandbox ``.ai/spec/what/step-cli.md``) reads one
``StepRequest`` JSON line on stdin: the ``POST /v1/agent/run`` fields (``query``,
``systemPrompt``, ``outputSchema``, ``context``, ``timeout_ms``) plus ``stepId``,
``runDir`` and ``traceparent``. Unknown keys are rejected (exit 2), so only those
keys are ever sent. Pure module: no I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import secrets
from dataclasses import dataclass
from typing import Any

from cloud_agents.runtime.tracing import inject_traceparent

logger = logging.getLogger(__name__)

RUN_ROOT = "/tmp/lsrun"
MAX_TIMEOUT_MS = 86_400_000  # StepRequest.timeout_ms le=
# The CLI reads at most 1 MiB including the newline, and the gateway caps the
# whole ExecSandboxRequest (stdin + env + argv) at 1 MiB. Keep the same 64 KiB
# headroom as OpenShellSpawner._MAX_EXEC_STDIN_BYTES.
MAX_STEP_REQUEST_BYTES = 1024 * 1024 - 64 * 1024

_STEP_ID = re.compile(r"[a-z0-9-]{1,40}")
_SLUG_SEP = re.compile(r"[^a-z0-9]+")
_SLUG_LEN = 27  # + "-" + 12 hex digest chars = 40


class StepRequestError(ValueError):
    """The step can't be sent to the step CLI; fail it without retrying."""


class StepRequestTooLarge(StepRequestError):
    """The serialized request exceeds MAX_STEP_REQUEST_BYTES."""


@dataclass(frozen=True)
class StepExecRequest:
    step_id: str
    run_dir: str
    stdin: bytes


def derive_step_id(workflow_id: str, step_name: str, attempt: int) -> str:
    """Derive a CLI ``stepId`` (``^[a-z0-9-]{1,40}$``) from a free-form step name.

    A readable slug of the name plus a hash of the exact (workflow, step, attempt),
    so names that slug the same still get distinct ids.
    """
    slug = _SLUG_SEP.sub("-", step_name.lower()).strip("-")[:_SLUG_LEN] or "step"
    key = json.dumps([workflow_id, step_name, attempt])
    return f"{slug}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def new_run_dir(step_id: str, run_root: str = RUN_ROOT) -> str:
    """Return a fresh, unguessable runDir path directly under run_root.

    The CLI requires runDir to be a direct child of its run root, compared as
    strings, so run_root must be absolute and normalized.
    """
    if not run_root.startswith("/") or posixpath.normpath(run_root) != run_root:
        raise ValueError(f"run_root must be an absolute normalized path: {run_root!r}")
    if not _STEP_ID.fullmatch(step_id):
        raise ValueError(f"invalid stepId: {step_id!r}")
    return f"{run_root}/{step_id}-{secrets.token_hex(8)}"


def build_step_request(
    step: dict[str, Any],
    *,
    workflow_id: str,
    attempt: int,
    context: dict[str, Any],
    system_prompt: str | None = None,
    timeout_seconds: int | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    run_root: str = RUN_ROOT,
) -> StepExecRequest:
    """Build the step CLI stdin for one step.

    Parameters:
        step: The step spec (``name``, ``prompt``, ``output_schema``, ``permissions``).
        workflow_id: Workflow ID, part of the stepId hash.
        attempt: Retry attempt, part of the stepId hash.
        context: Sandbox context, as sent to ``/run`` today.
        system_prompt: ``systemPrompt``, already interpolated by the caller.
        timeout_seconds: Sent as ``timeout_ms``; omitted means the CLI default.
        mcp_servers: Resolved MCP servers for the step. Only checked: the CLI reads
            MCP config from env, which the gateway logs a preview of, so servers
            with plain header values are rejected until the CLI accepts config on stdin.
        run_root: Parent of the runDir.

    Raises:
        StepRequestTooLarge: The request exceeds MAX_STEP_REQUEST_BYTES.
        StepRequestError: The step can't be expressed as a valid StepRequest.
    """
    step_name = step["name"]
    for server in mcp_servers or []:
        if any(server.get("headers") or {}):
            raise StepRequestError(
                f"Step '{step_name}': MCP server '{server.get('name')}' has plain headers, "
                "which the exec transport can't deliver safely; use secret_headers"
            )

    permissions = step.get("permissions") or {}
    if permissions.get("allowed_tools") or permissions.get("denied_tools"):
        logger.warning(
            "Step '%s': allowed_tools/denied_tools are not enforced on the exec transport "
            "(same as HTTP /run today); omitting them from the step request",
            step_name,
        )

    step_id = derive_step_id(workflow_id, step_name, attempt)
    run_dir = new_run_dir(step_id, run_root)
    request: dict[str, Any] = {
        "query": step.get("prompt", ""),
        "context": context,
        "stepId": step_id,
        "runDir": run_dir,
    }
    if system_prompt:
        request["systemPrompt"] = system_prompt
    if output_schema := step.get("output_schema"):
        request["outputSchema"] = output_schema
    if timeout_seconds is not None:
        timeout_ms = int(timeout_seconds * 1000)
        if not 0 < timeout_ms <= MAX_TIMEOUT_MS:
            raise StepRequestError(
                f"Step '{step_name}': timeout {timeout_seconds}s outside 0 < t <= "
                f"{MAX_TIMEOUT_MS // 1000}s"
            )
        request["timeout_ms"] = timeout_ms
    if traceparent := inject_traceparent({}).get("traceparent"):
        request["traceparent"] = traceparent

    try:
        stdin = (
            json.dumps(request, allow_nan=False, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (ValueError, TypeError) as exc:  # NaN, lone surrogates, non-JSON types
        raise StepRequestError(f"Step '{step_name}': request is not strict JSON: {exc}") from None
    if len(stdin) > MAX_STEP_REQUEST_BYTES:
        raise StepRequestTooLarge(
            f"Step '{step_name}': step request is {len(stdin)} bytes, over the "
            f"{MAX_STEP_REQUEST_BYTES}-byte limit"
        )
    return StepExecRequest(step_id=step_id, run_dir=run_dir, stdin=stdin)
