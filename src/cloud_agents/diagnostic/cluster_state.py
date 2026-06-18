"""Simulated cluster state for the diagnostic agent.

Provides mutable cluster state that diagnostic tools read and modify.
In Phase 1a this is a test harness; Phase 1b replaces it with real cluster APIs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

cluster_state: dict[str, Any] = {}
action_log: list[dict[str, Any]] = []


def reset_cluster_healthy() -> None:
    """Reset cluster to a fully healthy baseline."""
    cluster_state.clear()
    cluster_state.update({
        "hosts": {
            "web-01": {
                "role": "webserver",
                "cpu": 45,
                "memory": 62,
                "disk": 78,
                "status": "healthy",
                "services": {"nginx": "running", "app": "running"},
            },
            "web-02": {
                "role": "webserver",
                "cpu": 35,
                "memory": 50,
                "disk": 45,
                "status": "healthy",
                "services": {"nginx": "running", "app": "running"},
            },
            "db-01": {
                "role": "database",
                "cpu": 30,
                "memory": 55,
                "disk": 70,
                "status": "healthy",
                "services": {"postgresql": "running"},
            },
            "cache-01": {
                "role": "cache",
                "cpu": 15,
                "memory": 40,
                "disk": 30,
                "status": "healthy",
                "services": {"redis": "running"},
            },
        },
        "recent_deploys": [],
        "alerts": [],
    })
    action_log.clear()


def simulate_bad_deploy() -> None:
    """Simulate a bad deployment on web-02."""
    host = cluster_state["hosts"]["web-02"]
    host["cpu"] = 92
    host["memory"] = 88
    host["status"] = "degraded"
    host["services"]["app"] = "crashed"
    cluster_state["recent_deploys"].append(
        {
            "host": "web-02",
            "app": "frontend",
            "version": "v2.3.1",
            "time": datetime.now(timezone.utc).isoformat(),
            "status": "deployed",
        }
    )
    cluster_state["alerts"].append(
        "web-02: CPU spike 92%, app process crashed after deploy v2.3.1"
    )


def simulate_disk_growth(host: str, target_pct: int) -> None:
    """Simulate disk growing to a target percentage."""
    cluster_state["hosts"][host]["disk"] = target_pct
    if target_pct >= 90:
        cluster_state["hosts"][host]["status"] = "warning"
        cluster_state["alerts"].append(
            f"{host}: disk usage {target_pct}%, approaching critical threshold"
        )
