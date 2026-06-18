"""Monitoring agent tool implementations.

The monitoring agent has a single read-only tool for getting cluster state.
It does NOT have remediation tools — detection only.
"""

from __future__ import annotations

from typing import Any

from agents.diagnostic.cluster_state import cluster_state


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
