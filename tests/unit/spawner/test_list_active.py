"""Tests for spawner list_active() orphan detection."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from cloud_agents.spawner.kubernetes_spawner import KubernetesSpawner
from cloud_agents.spawner.podman_spawner import PodmanSpawner


class TestKubernetesListActive:
    """Tests for KubernetesSpawner._do_list_active."""

    @pytest.mark.asyncio
    async def test_list_active_returns_job_names(self) -> None:
        """list_active returns names of Jobs with matching labels."""
        mock_batch = MagicMock()

        job1 = MagicMock()
        job1.metadata.name = "agent-ca-abc123"
        job2 = MagicMock()
        job2.metadata.name = "agent-ca-def456"

        job_list = MagicMock()
        job_list.items = [job1, job2]
        mock_batch.list_namespaced_job.return_value = job_list

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="default")
            result = await spawner.list_active({"spawned-by": "workflow-runner"})

        assert result == ["ca-abc123", "ca-def456"]
        mock_batch.list_namespaced_job.assert_called_once_with(
            namespace="default",
            label_selector="spawned-by=workflow-runner",
        )

    @pytest.mark.asyncio
    async def test_list_active_empty_when_no_jobs(self) -> None:
        """list_active returns empty list when no Jobs match."""
        mock_batch = MagicMock()

        job_list = MagicMock()
        job_list.items = []
        mock_batch.list_namespaced_job.return_value = job_list

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="test-ns")
            result = await spawner.list_active({"spawned-by": "workflow-runner"})

        assert result == []

    @pytest.mark.asyncio
    async def test_list_active_no_labels(self) -> None:
        """list_active with no labels uses empty selector."""
        mock_batch = MagicMock()

        job_list = MagicMock()
        job_list.items = []
        mock_batch.list_namespaced_job.return_value = job_list

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="default")
            result = await spawner.list_active()

        assert result == []
        mock_batch.list_namespaced_job.assert_called_once_with(
            namespace="default",
            label_selector="",
        )


class TestPodmanListActive:
    """Tests for PodmanSpawner._do_list_active."""

    @pytest.mark.asyncio
    async def test_list_active_returns_container_names(self) -> None:
        """list_active returns names of containers with matching labels."""
        mock_container1 = MagicMock()
        mock_container1.name = "agent-my-agent-1"
        mock_container2 = MagicMock()
        mock_container2.name = "agent-my-agent-2"

        mock_client_instance = MagicMock()
        mock_client_instance.containers.list.return_value = [
            mock_container1,
            mock_container2,
        ]
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        mock_podman_module = MagicMock()
        mock_podman_module.PodmanClient.return_value = mock_client_instance

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner()
            result = await spawner.list_active({"spawned-by": "workflow-runner"})

        assert result == ["my-agent-1", "my-agent-2"]

    @pytest.mark.asyncio
    async def test_list_active_empty_when_no_containers(self) -> None:
        """list_active returns empty list when no containers match."""
        mock_client_instance = MagicMock()
        mock_client_instance.containers.list.return_value = []
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        mock_podman_module = MagicMock()
        mock_podman_module.PodmanClient.return_value = mock_client_instance

        with patch.dict(sys.modules, {"podman": mock_podman_module}):
            spawner = PodmanSpawner()
            result = await spawner.list_active({"spawned-by": "workflow-runner"})

        assert result == []

    @pytest.mark.asyncio
    async def test_list_active_without_podman_installed(self) -> None:
        """list_active returns empty list when podman-py is not installed."""
        spawner = PodmanSpawner()
        with patch.dict(sys.modules, {"podman": None}):
            result = await spawner.list_active({"spawned-by": "workflow-runner"})

        assert result == []
