"""Diagnostic agent tools — self-contained module for container deployment.

Mountable at /app/tools/diagnostic_tools.py in the agent-runtime container.
All cluster state and tool functions are inlined so this file has no
cross-module dependencies beyond the platform's agents.models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic_ai import ModelRetry, RunContext

from agents.models import DiagnosticReport

cluster_state: dict[str, Any] = {}
action_log: list[dict[str, Any]] = []


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
    action_log.clear()


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


def _ensure_initialized() -> None:
    """Lazily initialize cluster state if empty."""
    if not cluster_state:
        init_scenario("healthy")


def list_hosts() -> list[dict[str, Any]]:
    """List all hosts with their role and current status."""
    _ensure_initialized()
    return [
        {"hostname": k, "role": v["role"], "status": v["status"]}
        for k, v in cluster_state["hosts"].items()
    ]


def check_host(hostname: str) -> dict[str, Any]:
    """Get detailed status for a host including resource usage and services."""
    _ensure_initialized()
    host = cluster_state["hosts"].get(hostname)
    if not host:
        return {"error": f"Unknown host: {hostname}"}
    return {"hostname": hostname, **host}


def get_alerts() -> list[str]:
    """Get active cluster alerts."""
    _ensure_initialized()
    return cluster_state["alerts"]


def get_recent_deploys() -> list[dict[str, Any]]:
    """Get recent deployments across the cluster."""
    _ensure_initialized()
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
    """
    _ensure_initialized()
    host = cluster_state["hosts"].get(hostname)
    if not host:
        return {"success": False, "error": f"Unknown host: {hostname}"}

    if action.startswith("restart_service:"):
        service = action.split(":", 1)[1]
        if service in host["services"]:
            host["services"][service] = "running"
            if service == "app":
                host["cpu"] = max(host["cpu"] - 30, 35)
                host["memory"] = max(host["memory"] - 25, 45)
                host["status"] = "healthy"
            result = {"success": True, "message": f"Service {service} restarted on {hostname}"}
        else:
            result = {"success": False, "error": f"Service {service} not found on {hostname}"}
    elif action.startswith("rollback_deploy:"):
        app = action.split(":", 1)[1]
        rolled_back = False
        for deploy in cluster_state["recent_deploys"]:
            if deploy["host"] == hostname and deploy["app"] == app:
                deploy["status"] = "rolled_back"
                host["services"]["app"] = "running"
                host["cpu"] = 40
                host["memory"] = 55
                host["status"] = "healthy"
                result = {"success": True, "message": f"Rolled back {app} on {hostname}"}
                rolled_back = True
                break
        if not rolled_back:
            result = {"success": False, "error": f"No recent deploy of {app} on {hostname}"}
    elif action == "cleanup_disk":
        if host["disk"] > 70:
            host["disk"] = max(host["disk"] - 30, 45)
            if host["disk"] < 90:
                host["status"] = "healthy"
            result = {"success": True, "message": f"Cleaned disk on {hostname}, now at {host['disk']}%"}
        else:
            result = {"success": False, "error": "Disk usage already acceptable"}
    elif action == "scale_resources":
        host["cpu"] = max(host["cpu"] - 20, 20)
        host["memory"] = max(host["memory"] - 15, 30)
        if host["cpu"] < 80 and host["memory"] < 80:
            host["status"] = "healthy"
        result = {"success": True, "message": f"Scaled resources on {hostname}"}
    else:
        result = {"success": False, "error": f"Unknown action: {action}"}

    action_log.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "host": hostname, "action": action, "reason": reason, "result": result,
    })
    return result


async def verify_all_fixed(
    ctx: RunContext[None], report: DiagnosticReport
) -> DiagnosticReport:
    """Output validator — reject report if hosts are still broken."""
    unhealthy = [
        f"{name} ({h['status']})"
        for name, h in cluster_state["hosts"].items()
        if h["status"] != "healthy"
    ]
    if unhealthy and report.cluster_healthy:
        raise ModelRetry(
            f"Report claims healthy but these hosts are still unhealthy: "
            f"{', '.join(unhealthy)}. Investigate and fix them."
        )
    if unhealthy and not report.actions_taken:
        raise ModelRetry(
            "There are unhealthy hosts but no remediation actions taken. "
            "Use run_remediation tool to fix issues."
        )
    return report


__all__ = [
    "list_hosts",
    "check_host",
    "get_alerts",
    "get_recent_deploys",
    "run_remediation",
    "verify_all_fixed",
    "init_scenario",
    "cluster_state",
]
