"""Unit tests for skills image support in spawners."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


class TestKubernetesSpawnerSkills:
    """Tests for K8s spawner skills image init container."""

    @pytest.mark.asyncio
    async def test_skills_image_adds_init_container(
        self, mocker: MockerFixture,
    ) -> None:
        """Skills image adds an init container and shared volume."""
        mock_k8s_client = mocker.patch("kubernetes.client")
        mocker.patch("kubernetes.config")
        mock_batch = mocker.MagicMock()
        mock_core = mocker.MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core

        mock_k8s_client.V1Job = lambda **kw: type("Job", (), kw)()
        mock_k8s_client.V1ObjectMeta = lambda **kw: type("Meta", (), kw)()
        mock_k8s_client.V1JobSpec = lambda **kw: type("JobSpec", (), kw)()
        mock_k8s_client.V1PodTemplateSpec = lambda **kw: type("PodTemplate", (), kw)()
        mock_k8s_client.V1PodSpec = lambda **kw: type("PodSpec", (), kw)()
        mock_k8s_client.V1Container = lambda **kw: type("Container", (), kw)()
        mock_k8s_client.V1ContainerPort = lambda **kw: type("Port", (), kw)()
        mock_k8s_client.V1EnvVar = lambda **kw: type("EnvVar", (), kw)()
        mock_k8s_client.V1ResourceRequirements = lambda **kw: type("Resources", (), kw)()
        mock_k8s_client.V1Volume = lambda **kw: type("Volume", (), kw)()
        mock_k8s_client.V1VolumeMount = lambda **kw: type("VolumeMount", (), kw)()
        mock_k8s_client.V1EmptyDirVolumeSource = lambda **kw: type("EmptyDir", (), kw)()
        mock_k8s_client.V1Service = lambda **kw: type("Service", (), kw)()
        mock_k8s_client.V1ServiceSpec = lambda **kw: type("ServiceSpec", (), kw)()
        mock_k8s_client.V1ServicePort = lambda **kw: type("ServicePort", (), kw)()

        from agents.spawner.kubernetes_spawner import KubernetesSpawner
        spawner = KubernetesSpawner(namespace="test")

        await spawner._do_spawn(
            "test-agent", "sandbox:latest", {},
            skills_image="skills:v1",
            skills_paths=["/skills/diag"],
        )

        job_call = mock_batch.create_namespaced_job.call_args
        job_body = job_call[1]["body"]
        pod_spec = job_body.spec.template.spec

        init_containers = pod_spec.init_containers or []
        assert len(init_containers) == 1
        assert init_containers[0].image == "skills:v1"

    @pytest.mark.asyncio
    async def test_no_skills_no_init_container(
        self, mocker: MockerFixture,
    ) -> None:
        """No skills image means no init container."""
        mock_k8s_client = mocker.patch("kubernetes.client")
        mocker.patch("kubernetes.config")
        mock_batch = mocker.MagicMock()
        mock_core = mocker.MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core

        mock_k8s_client.V1Job = lambda **kw: type("Job", (), kw)()
        mock_k8s_client.V1ObjectMeta = lambda **kw: type("Meta", (), kw)()
        mock_k8s_client.V1JobSpec = lambda **kw: type("JobSpec", (), kw)()
        mock_k8s_client.V1PodTemplateSpec = lambda **kw: type("PodTemplate", (), kw)()
        mock_k8s_client.V1PodSpec = lambda **kw: type("PodSpec", (), kw)()
        mock_k8s_client.V1Container = lambda **kw: type("Container", (), kw)()
        mock_k8s_client.V1ContainerPort = lambda **kw: type("Port", (), kw)()
        mock_k8s_client.V1EnvVar = lambda **kw: type("EnvVar", (), kw)()
        mock_k8s_client.V1ResourceRequirements = lambda **kw: type("Resources", (), kw)()
        mock_k8s_client.V1Volume = lambda **kw: type("Volume", (), kw)()
        mock_k8s_client.V1VolumeMount = lambda **kw: type("VolumeMount", (), kw)()
        mock_k8s_client.V1Service = lambda **kw: type("Service", (), kw)()
        mock_k8s_client.V1ServiceSpec = lambda **kw: type("ServiceSpec", (), kw)()
        mock_k8s_client.V1ServicePort = lambda **kw: type("ServicePort", (), kw)()

        from agents.spawner.kubernetes_spawner import KubernetesSpawner
        spawner = KubernetesSpawner(namespace="test")

        await spawner._do_spawn("test-agent", "sandbox:latest", {})

        job_call = mock_batch.create_namespaced_job.call_args
        job_body = job_call[1]["body"]
        pod_spec = job_body.spec.template.spec

        assert pod_spec.init_containers is None
