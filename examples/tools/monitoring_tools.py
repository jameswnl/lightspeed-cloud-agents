"""Monitoring agent tools — self-contained module for container deployment.

Mountable at /app/tools/monitoring_tools.py in the agent-runtime container.
All cluster state and tool functions inlined for zero cross-module dependencies.
"""

from __future__ import annotations

from typing import Any

cluster_state: dict[str, Any] = {}


def _reset_cluster_healthy() -> None:
    """Reset cluster to a fully healthy baseline."""
    cluster_state.clear()
    cluster_state.update({
        "hosts": {
            "web-01": {
                "role": "webserver", "cpu": 45, "memory": 62, "disk": 78,
                "status": "healthy",
                "services": {"nginx": "running", "app": "running"},
            },
            "web-02": {
                "role": "webserver", "cpu": 35, "memory": 50, "disk": 45,
                "status": "healthy",
                "services": {"nginx": "running", "app": "running"},
            },
            "db-01": {
                "role": "database", "cpu": 30, "memory": 55, "disk": 70,
                "status": "healthy",
                "services": {"postgresql": "running"},
            },
            "cache-01": {
                "role": "cache", "cpu": 15, "memory": 40, "disk": 30,
                "status": "healthy",
                "services": {"redis": "running"},
            },
        },
        "recent_deploys": [],
        "alerts": [],
    })


def init_scenario(name: str) -> None:
    """Initialize cluster state for a named scenario."""
    _reset_cluster_healthy()
    if name == "healthy":
        return
    if name == "bad_deploy":
        host = cluster_state["hosts"]["web-02"]
        host["cpu"] = 92
        host["memory"] = 88
        host["status"] = "degraded"
        host["services"]["app"] = "crashed"
        cluster_state["recent_deploys"].append({
            "host": "web-02", "app": "frontend", "version": "v2.3.1",
            "time": "2026-06-17T14:25:00+00:00", "status": "deployed",
        })
        cluster_state["alerts"].append(
            "web-02: CPU spike 92%, app process crashed after deploy v2.3.1"
        )
        return
    raise ValueError(f"Unknown scenario: {name!r}")


def get_cluster_summary() -> list[dict[str, Any]]:
    """Get summary of all hosts with metrics and service status."""
    return [
        {
            "hostname": name,
            "role": h["role"],
            "status": h["status"],
            "cpu": h["cpu"],
            "memory": h["memory"],
            "disk": h["disk"],
            "services": h["services"],
        }
        for name, h in cluster_state["hosts"].items()
    ]


def mark_hosts_healthy(alerts: list[dict[str, Any]]) -> None:
    """Post-dispatch callback — reset affected hosts to healthy baseline."""
    for alert in alerts:
        host = cluster_state["hosts"].get(alert.get("host", ""))
        if host:
            host["status"] = "healthy"
            host["cpu"] = min(host["cpu"], 50)
            host["memory"] = min(host["memory"], 60)
            host["disk"] = min(host["disk"], 70)
            for svc in host.get("services", {}):
                host["services"][svc] = "running"


__all__ = [
    "get_cluster_summary",
    "mark_hosts_healthy",
    "init_scenario",
    "cluster_state",
]
