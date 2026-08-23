"""Built-in tool: kubectl_get -- read-only Kubernetes resource queries."""

from __future__ import annotations

import subprocess

from cloud_agents.workflow.executor.step.tools import step_tool


@step_tool("kubectl_get", description="Get Kubernetes resources (read-only)")
def kubectl_get(
    resource: str,
    namespace: str = "default",
    output_format: str = "json",
) -> str:
    """Get Kubernetes resources by type.

    Parameters:
        resource: Resource type (e.g. 'pods', 'deployments', 'services').
        namespace: Kubernetes namespace (default: 'default').
        output_format: Output format -- 'json', 'yaml', or 'wide' (default: 'json').

    Returns:
        kubectl output as string, or error message on failure.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", resource, "-n", namespace, "-o", output_format],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: kubectl command timed out after 30s"

    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout
