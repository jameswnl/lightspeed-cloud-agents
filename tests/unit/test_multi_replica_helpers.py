"""Unit tests for multi-replica E2E helper functions.

Tests the utility functions used by multi_replica_steps.py
without requiring a real Kind cluster. Uses subprocess mocking.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest


# Import the helpers from the steps module
from tests.e2e.features.steps.multi_replica_steps import (
    _get_ready_runner_pods,
    _get_runner_pods,
    _kubectl,
    _kubectl_json,
)


class TestKubectl:
    """Test the _kubectl helper function."""

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_returns_stdout(self, mock_run) -> None:
        """_kubectl returns stripped stdout."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  output  \n", stderr=""
        )
        result = _kubectl("get", "pods")
        assert result == "output"

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_passes_namespace(self, mock_run) -> None:
        """_kubectl includes -n namespace in command."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _kubectl("get", "pods")
        args = mock_run.call_args[0][0]
        assert "-n" in args
        assert "default" in args

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_raises_on_error(self, mock_run) -> None:
        """_kubectl raises CalledProcessError when check=True."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "kubectl")
        with pytest.raises(subprocess.CalledProcessError):
            _kubectl("get", "pods")

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_no_raise_when_check_false(self, mock_run) -> None:
        """_kubectl doesn't raise when check=False."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="error output", stderr=""
        )
        result = _kubectl("get", "pods", check=False)
        assert result == "error output"


class TestKubectlJson:
    """Test the _kubectl_json helper."""

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_json_parses_output(self, mock_run) -> None:
        """_kubectl_json returns parsed JSON."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"items": []}),
            stderr="",
        )
        result = _kubectl_json("get", "pods")
        assert result == {"items": []}

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_kubectl_json_adds_output_flag(self, mock_run) -> None:
        """_kubectl_json adds -o json to the command."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({}),
            stderr="",
        )
        _kubectl_json("get", "deployment", "workflow-runner")
        args = mock_run.call_args[0][0]
        assert "-o" in args
        assert "json" in args


class TestGetRunnerPods:
    """Test pod retrieval helpers."""

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_get_runner_pods_returns_items(self, mock_run) -> None:
        """_get_runner_pods returns pod items from kubectl."""
        pods_data = {
            "items": [
                {
                    "metadata": {"name": "runner-abc"},
                    "status": {"conditions": []},
                },
                {
                    "metadata": {"name": "runner-def"},
                    "status": {"conditions": []},
                },
            ]
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(pods_data),
            stderr="",
        )
        pods = _get_runner_pods()
        assert len(pods) == 2
        assert pods[0]["metadata"]["name"] == "runner-abc"

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_get_runner_pods_empty(self, mock_run) -> None:
        """_get_runner_pods returns empty list when no pods found."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"items": []}),
            stderr="",
        )
        pods = _get_runner_pods()
        assert pods == []

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_get_ready_runner_pods_filters_ready(self, mock_run) -> None:
        """_get_ready_runner_pods filters for Ready=True condition."""
        pods_data = {
            "items": [
                {
                    "metadata": {"name": "runner-ready"},
                    "status": {
                        "conditions": [
                            {"type": "Ready", "status": "True"},
                        ]
                    },
                },
                {
                    "metadata": {"name": "runner-not-ready"},
                    "status": {
                        "conditions": [
                            {"type": "Ready", "status": "False"},
                        ]
                    },
                },
                {
                    "metadata": {"name": "runner-no-conditions"},
                    "status": {"conditions": []},
                },
            ]
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(pods_data),
            stderr="",
        )
        ready = _get_ready_runner_pods()
        assert len(ready) == 1
        assert ready[0]["metadata"]["name"] == "runner-ready"

    @patch("tests.e2e.features.steps.multi_replica_steps.subprocess.run")
    def test_get_ready_runner_pods_all_ready(self, mock_run) -> None:
        """_get_ready_runner_pods returns all pods when all are ready."""
        pods_data = {
            "items": [
                {
                    "metadata": {"name": "runner-1"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                },
                {
                    "metadata": {"name": "runner-2"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                },
            ]
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(pods_data),
            stderr="",
        )
        ready = _get_ready_runner_pods()
        assert len(ready) == 2
