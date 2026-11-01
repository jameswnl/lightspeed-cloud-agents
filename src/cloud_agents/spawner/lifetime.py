"""Sandbox expiry labels and the enforced deadlines they're computed from.

Shared by OpenShellSpawner (which stamps the labels at spawn time),
reconcile_orphaned_sandboxes() (which filters on them at startup) and the
spawn call sites that compute each sandbox's lifetime. Kept dependency-free
so the Temporal workflow module can import it.

A sandbox's cloud-agents/expires-at label must never be earlier than its
latest legitimate use, or another replica's startup reconcile could destroy
it while still in use. Each workflow step therefore passes a lifetime equal
to the sum of the wall-clock deadlines enforced on everything that can use
the sandbox after expires-at is stamped (spawn budget, run call, transcript
fetch and save) plus destroy grace; the spawner adds a clock-skew margin.
Lifecycles without an enforced latest-use bound (e.g. CLI sessions) pass no
lifetime and get no expiry, so startup reconcile never reaps them.

Spawn and each post-spawn I/O phase run through await_within(): the work runs as a
task that's waited on for at most its deadline, then cancelled and waited on
for at most SANDBOX_CANCEL_GRACE_SECONDS. Cooperative I/O (httpx, asyncpg,
asyncio) stops within that grace. A task still running after it is
uncooperative: it's abandoned (logged, never awaited again) and the caller
fails or moves on without touching the sandbox from that phase again.
Python can't preempt arbitrary code, so abandoned work may keep running in
the background; the bound is on how long the step keeps using the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Attached to every sandbox so reconcile can find sandboxes spawned by a
# previous process instance via ListSandboxes(label_selector=...) (issue #224).
SPAWNED_BY_LABEL: dict[str, str] = {"spawned-by": "workflow-runner"}
# Unix seconds after which any replica's reconcile may destroy the sandbox.
EXPIRES_AT_LABEL_KEY = "cloud-agents/expires-at"
# Written by an earlier, unreleased owner-scoping design; never stamped now.
RETIRED_OWNER_INSTANCE_LABEL_KEY = "cloud-agents/owner-instance"
# Set only by the spawner (or not at all); dropped from caller labels.
RESERVED_LABEL_KEYS = frozenset({EXPIRES_AT_LABEL_KEY, RETIRED_OWNER_INSTANCE_LABEL_KEY})

# Step run timeout (permissions.timeout_seconds) when unset; also the Temporal
# start_to_close_timeout default for a step without timeout_seconds.
DEFAULT_STEP_TIMEOUT_SECONDS = 600
# OpenShellSpawner._do_spawn: gateway wait_ready after CreateSandbox ...
SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS = 300
# ... then the gateway-proxied HTTP health check.
SANDBOX_HTTP_READY_TIMEOUT_SECONDS = 60
# Overall budget for OpenShellSpawner._do_spawn, starting when expires-at is
# stamped and covering CreateSandbox (30s SDK RPC timeout), the gateway ready
# wait, the HTTP health check, and 30s for post-create exec/expose calls.
SANDBOX_SPAWN_DEADLINE_SECONDS = (
    30 + SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS + SANDBOX_HTTP_READY_TIMEOUT_SECONDS + 30
)
# run_sandbox_step's heartbeat_timeout (temporal/workflow.py). Not part of the
# lifetime: the run call is bounded locally, not by heartbeat cancellation.
SANDBOX_ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 180
# Wall-clock bound on the post-run GET /v1/agent/events transcript fetch ...
SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS = 30
# ... and on persisting it to the transcript store, before destroy.
SANDBOX_TRANSCRIPT_SAVE_TIMEOUT_SECONDS = 30
# After a phase's deadline: how long to wait for its cancelled task to
# finish (e.g. an HTTP client's close) before abandoning it.
SANDBOX_CANCEL_GRACE_SECONDS = 5
# Bound on each best-effort cleanup step (sandbox delete, provider delete)
# after a failed spawn. Not part of the lifetime: a sandbox whose spawn has
# failed is no longer used, so reaping it mid-cleanup is harmless. This only
# keeps a stuck cleanup from blocking the caller.
SANDBOX_SPAWN_CLEANUP_TIMEOUT_SECONDS = 30
# Margin for the remaining in-process work and the finally-block destroy();
# not a safety requirement, since a sandbox is no longer used once the
# transcript save has finished.
SANDBOX_DESTROY_GRACE_SECONDS = 120


def run_timeout_seconds(step: dict[str, Any]) -> float:
    """Wall-clock deadline on a step's POST /v1/agent/run call."""
    permissions = step.get("permissions") or {}
    return float(permissions.get("timeout_seconds", DEFAULT_STEP_TIMEOUT_SECONDS))


def step_sandbox_lifetime_seconds(step: dict[str, Any]) -> int:
    """Latest legitimate use of one workflow step attempt's sandbox, measured
    from when expires-at is stamped: the sum of the enforced spawn, run,
    transcript fetch and transcript save deadlines, a cancel grace for each
    of the five await_within() phases (spawn, run, progress cleanup,
    transcript fetch, transcript save), plus destroy grace. Each retry attempt spawns a
    fresh sandbox (attempt is part of compute_pod_name()), so this is per
    attempt.
    """
    return (
        SANDBOX_SPAWN_DEADLINE_SECONDS
        + math.ceil(run_timeout_seconds(step))
        + SANDBOX_TRANSCRIPT_TIMEOUT_SECONDS
        + SANDBOX_TRANSCRIPT_SAVE_TIMEOUT_SECONDS
        + 5 * SANDBOX_CANCEL_GRACE_SECONDS
        + SANDBOX_DESTROY_GRACE_SECONDS
    )


class SandboxDeadlineExceeded(TimeoutError):
    """Raised by await_within() when its own deadline expired, as opposed to
    a TimeoutError raised by the awaited work itself."""


async def await_within(work: Awaitable[T], timeout: float, *, what: str) -> T:
    """Await work for at most timeout + SANDBOX_CANCEL_GRACE_SECONDS.

    Runs work as a task rather than under asyncio.timeout(): a timeout fires
    once, so cleanup that runs after it (e.g. a client __aexit__) would get
    no bound of its own.

    Raises:
        SandboxDeadlineExceeded: If work didn't finish within timeout. It is
            cancelled and, if still running after the cancel grace, abandoned.
    """
    task = asyncio.ensure_future(work)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await cancel_within(task, what=what)
        raise
    if task in done:
        return task.result()
    await cancel_within(task, what=what)
    raise SandboxDeadlineExceeded(f"{what} timed out after {timeout:g}s")


async def cancel_within(task: asyncio.Future[Any], *, what: str) -> None:
    """Cancel task and wait at most SANDBOX_CANCEL_GRACE_SECONDS for it.

    A task that hasn't finished by then is abandoned: logged and never
    awaited again. Its eventual result or error is consumed, not raised.
    """
    task.cancel()
    # Attached before waiting, so the outcome is consumed even if this wait
    # is itself cancelled (consuming twice is harmless).
    task.add_done_callback(_consume)
    try:
        done, _ = await asyncio.wait({task}, timeout=SANDBOX_CANCEL_GRACE_SECONDS)
    except asyncio.CancelledError:
        if not task.done():
            logger.warning("%s still running when its caller was cancelled — abandoning it", what)
        raise
    if task not in done:
        logger.warning(
            "%s did not stop within %ss of cancellation — abandoning it",
            what,
            SANDBOX_CANCEL_GRACE_SECONDS,
        )


def _consume(task: asyncio.Future[Any]) -> None:
    """Retrieve a finished task's outcome so asyncio doesn't log it as lost."""
    if not task.cancelled():
        task.exception()
