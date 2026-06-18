"""Diagnostic agent tool implementations.

These tools operate on the simulated cluster state. In Phase 1b,
they would be replaced with real cluster API calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agents.diagnostic.cluster_state import action_log, cluster_state


def list_hosts() -> list[dict[str, Any]]:
    """List all hosts with their role and current status."""
    return [
        {"hostname": k, "role": v["role"], "status": v["status"]}
        for k, v in cluster_state["hosts"].items()
    ]


def check_host(hostname: str) -> dict[str, Any]:
    """Get detailed status for a host including resource usage and services."""
    host = cluster_state["hosts"].get(hostname)
    if not host:
        return {"error": f"Unknown host: {hostname}"}
    return {"hostname": hostname, **host}


def get_alerts() -> list[str]:
    """Get active cluster alerts."""
    return cluster_state["alerts"]


def get_recent_deploys() -> list[dict[str, Any]]:
    """Get recent deployments across the cluster."""
    return cluster_state["recent_deploys"]


def run_remediation(hostname: str, action: str, reason: str) -> dict[str, Any]:
    """Run a remediation action on a host.

    Available actions:
    - restart_service:<service_name>
    - rollback_deploy:<app_name>
    - cleanup_disk
    - scale_resources

    Args:
        hostname: Target host.
        action: One of the available actions.
        reason: Why this remediation is needed.

    Returns:
        Dict with success status and message or error.
    """
    host = cluster_state["hosts"].get(hostname)
    if not host:
        return {"success": False, "error": f"Unknown host: {hostname}"}

    result = _apply_remediation(hostname, host, action)
    action_log.append(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "host": hostname,
            "action": action,
            "reason": reason,
            "result": result,
        }
    )
    return result


def _apply_remediation(
    hostname: str, host: dict[str, Any], action: str
) -> dict[str, Any]:
    """Apply a remediation action to the simulated cluster state."""
    if action.startswith("restart_service:"):
        service = action.split(":", 1)[1]
        if service in host["services"]:
            host["services"][service] = "running"
            if service == "app":
                host["cpu"] = max(host["cpu"] - 30, 35)
                host["memory"] = max(host["memory"] - 25, 45)
                host["status"] = "healthy"
            return {
                "success": True,
                "message": f"Service {service} restarted on {hostname}",
            }
        return {"success": False, "error": f"Service {service} not found on {hostname}"}

    if action.startswith("rollback_deploy:"):
        app = action.split(":", 1)[1]
        for deploy in cluster_state["recent_deploys"]:
            if deploy["host"] == hostname and deploy["app"] == app:
                deploy["status"] = "rolled_back"
                host["services"]["app"] = "running"
                host["cpu"] = 40
                host["memory"] = 55
                host["status"] = "healthy"
                return {
                    "success": True,
                    "message": f"Rolled back {app} on {hostname}",
                }
        return {
            "success": False,
            "error": f"No recent deploy of {app} on {hostname}",
        }

    if action == "cleanup_disk":
        if host["disk"] > 70:
            host["disk"] = max(host["disk"] - 30, 45)
            if host["disk"] < 90:
                host["status"] = "healthy"
            return {
                "success": True,
                "message": f"Cleaned disk on {hostname}, now at {host['disk']}%",
            }
        return {"success": False, "error": "Disk usage already acceptable"}

    if action == "scale_resources":
        host["cpu"] = max(host["cpu"] - 20, 20)
        host["memory"] = max(host["memory"] - 15, 30)
        if host["cpu"] < 80 and host["memory"] < 80:
            host["status"] = "healthy"
        return {"success": True, "message": f"Scaled resources on {hostname}"}

    return {"success": False, "error": f"Unknown action: {action}"}
