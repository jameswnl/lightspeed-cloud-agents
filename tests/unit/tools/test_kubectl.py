"""Tests for the kubectl_get built-in tool."""

from __future__ import annotations

import subprocess
from unittest.mock import patch


class TestKubectlGetRegistration:
    """Tests that kubectl_get is properly registered."""

    def test_registered_after_import(self) -> None:
        """Importing kubectl module registers 'kubectl_get' tool."""
        from cloud_agents.workflow.executor.step.tools import list_tools

        import cloud_agents.tools.kubectl  # noqa: F401

        assert "kubectl_get" in list_tools()


class TestKubectlGet:
    """Tests for kubectl_get tool function behavior."""

    def test_returns_json_output_on_success(self) -> None:
        """kubectl_get returns stdout on successful kubectl call."""
        from cloud_agents.tools.kubectl import kubectl_get

        fake_output = '{"items": []}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_output, stderr=""
            )
            result = kubectl_get("pods")

        assert result == fake_output
        mock_run.assert_called_once_with(
            ["kubectl", "get", "pods", "-n", "default", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_returns_error_on_failure(self) -> None:
        """kubectl_get returns error string when kubectl fails."""
        from cloud_agents.tools.kubectl import kubectl_get

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: the server doesn't have a resource type 'foo'",
            )
            result = kubectl_get("foo")

        assert result.startswith("Error:")
        assert "foo" in result

    def test_custom_namespace(self) -> None:
        """kubectl_get passes custom namespace to kubectl."""
        from cloud_agents.tools.kubectl import kubectl_get

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="{}", stderr=""
            )
            kubectl_get("pods", namespace="kube-system")

        mock_run.assert_called_once_with(
            ["kubectl", "get", "pods", "-n", "kube-system", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_custom_output_format(self) -> None:
        """kubectl_get passes custom output format to kubectl."""
        from cloud_agents.tools.kubectl import kubectl_get

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="NAME  READY", stderr=""
            )
            kubectl_get("pods", output_format="wide")

        mock_run.assert_called_once_with(
            ["kubectl", "get", "pods", "-n", "default", "-o", "wide"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_timeout_enforcement(self) -> None:
        """kubectl_get handles subprocess timeout."""
        from cloud_agents.tools.kubectl import kubectl_get

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30)
            result = kubectl_get("pods")

        assert "timed out" in result.lower()
