"""Diagnostic agent tools — standalone module for generic runtime.

Mountable at /app/tools/diagnostic_tools.py in the agent-runtime container.
Re-exports tools from the agents.diagnostic package and provides a
standalone output validator.
"""

from pydantic_ai import ModelRetry, RunContext

from examples.agents.diagnostic.cluster_state import cluster_state
from examples.agents.diagnostic.tools import (
    check_host,
    get_alerts,
    get_recent_deploys,
    list_hosts,
    run_remediation,
)
from agents.models import DiagnosticReport


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
]
