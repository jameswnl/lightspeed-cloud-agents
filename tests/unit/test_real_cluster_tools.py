"""Unit tests for real cluster API tools."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_TOOLS_PATH = Path(__file__).resolve().parents[3] / "agents" / "tools" / "real_cluster_tools.py"
spec = importlib.util.spec_from_file_location("real_cluster_tools", _TOOLS_PATH)
real_cluster_tools = importlib.util.module_from_spec(spec)
sys.modules["real_cluster_tools"] = real_cluster_tools
spec.loader.exec_module(real_cluster_tools)

list_nodes = real_cluster_tools.list_nodes
list_pods = real_cluster_tools.list_pods
get_pod_logs = real_cluster_tools.get_pod_logs
get_node_conditions = real_cluster_tools.get_node_conditions
check_cluster_health = real_cluster_tools.check_cluster_health


def _mock_node(name: str, ready: str = "True", cpu: str = "4", memory: str = "8Gi"):
    """Create a mock K8s node."""
    node = MagicMock()
    node.metadata.name = name
    condition = MagicMock()
    condition.type = "Ready"
    condition.status = ready
    condition.reason = "KubeletReady"
    condition.message = "kubelet is ready"
    node.status.conditions = [condition]
    node.status.allocatable = {"cpu": cpu, "memory": memory}
    return node


def _mock_pod(name: str, namespace: str = "default", phase: str = "Running", restarts: int = 0):
    """Create a mock K8s pod."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.status.phase = phase
    cs = MagicMock()
    cs.restart_count = restarts
    pod.status.container_statuses = [cs]
    return pod


class TestListNodes:
    """Tests for list_nodes."""

    @patch("real_cluster_tools._get_core_api")
    def test_lists_nodes(self, mock_api_fn) -> None:
        """Test listing cluster nodes."""
        api = MagicMock()
        api.list_node.return_value = MagicMock(items=[
            _mock_node("node-1"), _mock_node("node-2"),
        ])
        mock_api_fn.return_value = api
        result = list_nodes()
        assert "node-1" in result
        assert "node-2" in result
        assert "Ready=True" in result

    @patch("real_cluster_tools._get_core_api")
    def test_handles_error(self, mock_api_fn) -> None:
        """Test graceful error handling."""
        mock_api_fn.side_effect = RuntimeError("no cluster")
        result = list_nodes()
        assert "Error" in result


class TestListPods:
    """Tests for list_pods."""

    @patch("real_cluster_tools._get_core_api")
    def test_lists_pods(self, mock_api_fn) -> None:
        """Test listing pods in a namespace."""
        api = MagicMock()
        api.list_namespaced_pod.return_value = MagicMock(items=[
            _mock_pod("web-1"), _mock_pod("db-1"),
        ])
        mock_api_fn.return_value = api
        result = list_pods("default")
        assert "web-1" in result
        assert "db-1" in result


class TestGetPodLogs:
    """Tests for get_pod_logs."""

    @patch("real_cluster_tools._get_core_api")
    def test_gets_logs(self, mock_api_fn) -> None:
        """Test getting pod logs."""
        api = MagicMock()
        api.read_namespaced_pod_log.return_value = "line1\nline2\n"
        mock_api_fn.return_value = api
        result = get_pod_logs("web-1")
        assert "line1" in result


class TestGetNodeConditions:
    """Tests for get_node_conditions."""

    @patch("real_cluster_tools._get_core_api")
    def test_gets_conditions(self, mock_api_fn) -> None:
        """Test getting node conditions."""
        api = MagicMock()
        api.read_node.return_value = _mock_node("node-1")
        mock_api_fn.return_value = api
        result = get_node_conditions("node-1")
        assert "Ready" in result
        assert "KubeletReady" in result


class TestCheckClusterHealth:
    """Tests for check_cluster_health."""

    @patch("real_cluster_tools._get_core_api")
    def test_healthy_cluster(self, mock_api_fn) -> None:
        """Test health check on a healthy cluster."""
        api = MagicMock()
        api.list_node.return_value = MagicMock(items=[
            _mock_node("node-1"), _mock_node("node-2"),
        ])
        api.list_pod_for_all_namespaces.return_value = MagicMock(items=[
            _mock_pod("web-1"), _mock_pod("db-1"),
        ])
        mock_api_fn.return_value = api
        result = check_cluster_health()
        assert "2/2 nodes ready" in result
        assert "No issues" in result

    @patch("real_cluster_tools._get_core_api")
    def test_unhealthy_node(self, mock_api_fn) -> None:
        """Test health check with a not-ready node."""
        api = MagicMock()
        api.list_node.return_value = MagicMock(items=[
            _mock_node("node-1"), _mock_node("node-2", ready="False"),
        ])
        api.list_pod_for_all_namespaces.return_value = MagicMock(items=[])
        mock_api_fn.return_value = api
        result = check_cluster_health()
        assert "1/2 nodes ready" in result
        assert "node-2" in result

    @patch("real_cluster_tools._get_core_api")
    def test_crashing_pod(self, mock_api_fn) -> None:
        """Test health check with a crash-looping pod."""
        api = MagicMock()
        api.list_node.return_value = MagicMock(items=[_mock_node("node-1")])
        api.list_pod_for_all_namespaces.return_value = MagicMock(items=[
            _mock_pod("crash-pod", restarts=10),
        ])
        mock_api_fn.return_value = api
        result = check_cluster_health()
        assert "crash-pod" in result
        assert "restarts=10" in result
