"""Real cluster API tools for cloud agents.

Provides tool functions that query actual Kubernetes/OpenShift cluster
APIs for node status, pod health, and resource usage. Falls back to
error messages when the kubernetes client is unavailable.

These tools are designed to be mounted into agent definitions via
agent.yaml as an alternative to the simulated diagnostic_tools.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from kubernetes import client, config

    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False
    logger.info("kubernetes client not available — real cluster tools will return errors")


def _get_core_api() -> Any:
    """Get a Kubernetes CoreV1Api client."""
    if not _K8S_AVAILABLE:
        raise RuntimeError("kubernetes client is not installed")
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def list_nodes() -> str:
    """List all cluster nodes with their status and resource usage.

    Returns:
        Formatted string describing each node's status, conditions,
        and allocatable resources. Returns error message if K8s API
        is unavailable.
    """
    try:
        api = _get_core_api()
        nodes = api.list_node()
    except Exception as exc:
        return f"Error listing nodes: {exc}"

    lines = []
    for node in nodes.items:
        name = node.metadata.name
        conditions = {c.type: c.status for c in (node.status.conditions or [])}
        ready = conditions.get("Ready", "Unknown")
        cpu = node.status.allocatable.get("cpu", "?")
        memory = node.status.allocatable.get("memory", "?")
        lines.append(f"- {name}: Ready={ready}, CPU={cpu}, Memory={memory}")

    return f"Cluster nodes ({len(lines)}):\n" + "\n".join(lines)


def list_pods(namespace: str = "default") -> str:
    """List pods in a namespace with their status.

    Args:
        namespace: Kubernetes namespace to query.

    Returns:
        Formatted string describing each pod's status and restart count.
    """
    try:
        api = _get_core_api()
        pods = api.list_namespaced_pod(namespace)
    except Exception as exc:
        return f"Error listing pods in {namespace}: {exc}"

    lines = []
    for pod in pods.items:
        name = pod.metadata.name
        phase = pod.status.phase
        restarts = sum(
            (cs.restart_count or 0)
            for cs in (pod.status.container_statuses or [])
        )
        lines.append(f"- {name}: phase={phase}, restarts={restarts}")

    return f"Pods in {namespace} ({len(lines)}):\n" + "\n".join(lines)


def get_pod_logs(pod_name: str, namespace: str = "default", tail_lines: int = 50) -> str:
    """Get recent logs from a pod.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.
        tail_lines: Number of recent log lines to fetch.

    Returns:
        Recent log output from the pod.
    """
    try:
        api = _get_core_api()
        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )
    except Exception as exc:
        return f"Error getting logs for {pod_name}: {exc}"

    return f"Logs for {pod_name} (last {tail_lines} lines):\n{logs}"


def get_node_conditions(node_name: str) -> str:
    """Get detailed conditions for a specific node.

    Args:
        node_name: Name of the cluster node.

    Returns:
        Formatted string with all node conditions and their details.
    """
    try:
        api = _get_core_api()
        node = api.read_node(node_name)
    except Exception as exc:
        return f"Error reading node {node_name}: {exc}"

    lines = []
    for condition in (node.status.conditions or []):
        lines.append(
            f"- {condition.type}: {condition.status} "
            f"(reason={condition.reason}, message={condition.message})"
        )

    return f"Conditions for {node_name}:\n" + "\n".join(lines)


def check_cluster_health() -> str:
    """Run a quick health check across the cluster.

    Returns:
        Summary of cluster health including node readiness,
        pod status across all namespaces, and any detected issues.
    """
    issues = []

    try:
        api = _get_core_api()
        nodes = api.list_node()
    except Exception as exc:
        return f"Cannot assess cluster health: {exc}"

    not_ready = []
    for node in nodes.items:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}
        if conditions.get("Ready") != "True":
            not_ready.append(node.metadata.name)

    if not_ready:
        issues.append(f"Nodes not ready: {', '.join(not_ready)}")

    try:
        pods = api.list_pod_for_all_namespaces()
        failing_pods = []
        for pod in pods.items:
            if pod.status.phase in ("Failed", "Unknown"):
                failing_pods.append(f"{pod.metadata.namespace}/{pod.metadata.name}")
            for cs in (pod.status.container_statuses or []):
                if (cs.restart_count or 0) > 5:
                    failing_pods.append(
                        f"{pod.metadata.namespace}/{pod.metadata.name} "
                        f"(restarts={cs.restart_count})"
                    )
        if failing_pods:
            issues.append(f"Problem pods: {', '.join(failing_pods[:10])}")
    except Exception as exc:
        issues.append(f"Could not check pods: {exc}")

    total_nodes = len(nodes.items)
    healthy_nodes = total_nodes - len(not_ready)

    summary = f"Cluster health: {healthy_nodes}/{total_nodes} nodes ready"
    if issues:
        summary += f"\nIssues ({len(issues)}):\n" + "\n".join(f"  - {i}" for i in issues)
    else:
        summary += "\nNo issues detected."

    return summary
