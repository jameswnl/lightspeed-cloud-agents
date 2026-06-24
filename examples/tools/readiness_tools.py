"""Deployment readiness tools — a completely new agent type.

Proves the generic template: new agent, new tools, new output type,
zero changes to the platform code. Just mount this + agent.yaml.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from examples.agents.diagnostic.cluster_state import cluster_state


class DeploymentReadiness(BaseModel):
    """Output type for the deployment readiness agent."""

    ready: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommendation: str


def check_resource_capacity() -> list[dict[str, Any]]:
    """Check resource headroom across all hosts.

    Returns each host with its current usage and whether it has
    enough capacity for a new deployment (CPU < 70%, memory < 75%, disk < 80%).
    """
    results = []
    for name, h in cluster_state["hosts"].items():
        results.append({
            "hostname": name,
            "cpu": h["cpu"],
            "memory": h["memory"],
            "disk": h["disk"],
            "has_capacity": h["cpu"] < 70 and h["memory"] < 75 and h["disk"] < 80,
        })
    return results


def check_active_incidents() -> dict[str, Any]:
    """Check for active alerts or recent failed deployments."""
    alerts = cluster_state.get("alerts", [])
    recent_failures = [
        d for d in cluster_state.get("recent_deploys", [])
        if d.get("status") == "deployed"
    ]
    return {
        "active_alerts": len(alerts),
        "alert_details": alerts,
        "recent_deploys_in_progress": len(recent_failures),
        "has_incidents": len(alerts) > 0,
    }


def check_service_health() -> list[dict[str, Any]]:
    """Check if all services across all hosts are running."""
    results = []
    for name, h in cluster_state["hosts"].items():
        for svc, status in h.get("services", {}).items():
            results.append({
                "hostname": name,
                "service": svc,
                "status": status,
                "healthy": status == "running",
            })
    return results
