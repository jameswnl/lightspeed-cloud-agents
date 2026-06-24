"""Monitoring agent tools — standalone module for generic runtime.

Mountable at /app/tools/monitoring_tools.py in the agent-runtime container.
"""

from typing import Any

from examples.agents.diagnostic.cluster_state import cluster_state
from examples.agents.monitoring.tools import get_cluster_summary


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
]
