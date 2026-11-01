"""Sandbox ownership labels and lifetime bounds for orphan reconciliation.

Shared by OpenShellSpawner (which stamps the labels at spawn time) and
reconcile_orphaned_sandboxes() (which filters on them at startup), and by the
spawn call sites that compute each sandbox's lifetime. Kept dependency-free so
the Temporal workflow module can import it.

A sandbox's cloud-agents/expires-at label must never be earlier than its
latest legitimate end, or another replica's startup reconcile could destroy
it while still in use. Each spawn call site therefore passes a per-sandbox
lifetime_seconds covering everything that can keep the sandbox alive.
"""

from __future__ import annotations

import math
from typing import Any

# Attached to every sandbox so reconcile can find sandboxes spawned by a
# previous process instance via ListSandboxes(label_selector=...) (issue #224).
SPAWNED_BY_LABEL: dict[str, str] = {"spawned-by": "workflow-runner"}
# Runner replica that spawned the sandbox; reconcile destroys its own.
OWNER_INSTANCE_LABEL_KEY = "cloud-agents/owner-instance"
# Unix seconds after which any replica's reconcile may destroy the sandbox.
EXPIRES_AT_LABEL_KEY = "cloud-agents/expires-at"
# Set only by the spawner from its own config; dropped from caller labels.
RESERVED_LABEL_KEYS = frozenset({OWNER_INSTANCE_LABEL_KEY, EXPIRES_AT_LABEL_KEY})

# Step timeout when neither the step nor its definition sets one; becomes the
# run_sandbox_step activity's start_to_close_timeout (temporal/workflow.py)
# and the default sandbox HTTP timeout (permissions.timeout_seconds).
DEFAULT_STEP_TIMEOUT_SECONDS = 600
# OpenShellSpawner._do_spawn: gateway wait_ready after CreateSandbox ...
SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS = 300
# ... then the gateway-proxied HTTP health check.
SANDBOX_HTTP_READY_TIMEOUT_SECONDS = 60
# run_sandbox_step's heartbeat_timeout (temporal/workflow.py): cancellation
# after start_to_close_timeout reaches the activity only via a heartbeat.
SANDBOX_ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 180
# Time for the finally-block destroy() to run after the step ends.
SANDBOX_DESTROY_GRACE_SECONDS = 120


def step_sandbox_lifetime_seconds(step: dict[str, Any]) -> int:
    """Latest legitimate life of one workflow step attempt's sandbox.

    The step's run time is bounded by the larger of its timeout_seconds
    (Temporal start_to_close_timeout) and permissions.timeout_seconds (the
    sandbox HTTP call timeout, the only bound on the local engine), plus the
    spawn readiness waits, the heartbeat delay before a timed-out activity
    sees cancellation, and destroy grace. Each retry attempt spawns a fresh
    sandbox (attempt is part of compute_pod_name()), so this is per attempt.
    """
    permissions = step.get("permissions") or {}
    run_seconds = max(
        step.get("timeout_seconds") or DEFAULT_STEP_TIMEOUT_SECONDS,
        permissions.get("timeout_seconds") or DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    return (
        math.ceil(run_seconds)
        + SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS
        + SANDBOX_HTTP_READY_TIMEOUT_SECONDS
        + SANDBOX_ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS
        + SANDBOX_DESTROY_GRACE_SECONDS
    )


def cli_session_lifetime_seconds(max_session_seconds: int, check_interval: float) -> int:
    """Latest legitimate life of a CLI session sandbox.

    CLISessionLauncher's timeout monitor destroys a session within one check
    interval after max_session_seconds; add the spawn readiness waits and
    destroy grace.
    """
    return (
        max_session_seconds
        + math.ceil(check_interval)
        + SANDBOX_GATEWAY_READY_TIMEOUT_SECONDS
        + SANDBOX_HTTP_READY_TIMEOUT_SECONDS
        + SANDBOX_DESTROY_GRACE_SECONDS
    )
