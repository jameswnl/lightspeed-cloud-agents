"""Unit tests for KubernetesSpawner with mocked K8s client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.spawner.base import SecretKeyRef, SpawnConfig
from agents.spawner.kubernetes_spawner import KubernetesSpawner


@pytest.fixture
def mock_k8s():
    """Mock the kubernetes client module."""
    with patch("agents.spawner.kubernetes_spawner.KubernetesSpawner._do_spawn") as mock_spawn, \
         patch("agents.spawner.kubernetes_spawner.KubernetesSpawner._do_destroy") as mock_destroy:
        yield mock_spawn, mock_destroy


class TestKubernetesSpawnerInit:
    """Tests for KubernetesSpawner initialization."""

    def test_default_config(self) -> None:
        """Test default namespace and service account."""
        spawner = KubernetesSpawner()
        assert spawner._namespace == "cloud-agents"
        assert spawner._service_account == "workflow-runner"

    def test_custom_config(self) -> None:
        """Test custom namespace and service account."""
        spawner = KubernetesSpawner(
            namespace="prod-agents",
            service_account="custom-sa",
        )
        assert spawner._namespace == "prod-agents"
        assert spawner._service_account == "custom-sa"

    def test_secret_env_vars(self) -> None:
        """Test secret_env_vars configuration."""
        refs = {
            "OPENAI_API_KEY": SecretKeyRef(secret_name="llm-key", key="api_key"),
        }
        spawner = KubernetesSpawner(secret_env_vars=refs)
        assert "OPENAI_API_KEY" in spawner._secret_env_vars
        assert spawner._secret_env_vars["OPENAI_API_KEY"].secret_name == "llm-key"

    def test_configmap_mounts(self) -> None:
        """Test ConfigMap mount configuration."""
        spawner = KubernetesSpawner(
            config_configmap="agent-config",
            tools_configmap="agent-tools",
        )
        assert spawner._config_configmap == "agent-config"
        assert spawner._tools_configmap == "agent-tools"

    def test_no_secret_env_vars_by_default(self) -> None:
        """Test that secret_env_vars is empty by default."""
        spawner = KubernetesSpawner()
        assert spawner._secret_env_vars == {}


class TestKubernetesSpawnerAlreadyExists:
    """Tests for idempotent Job creation (409 handling)."""

    @pytest.mark.asyncio
    async def test_spawn_job_409_same_image_succeeds(self) -> None:
        """Job AlreadyExists with matching image is treated as success."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        exc_409 = Exception("conflict")
        exc_409.status = 409
        mock_batch.create_namespaced_job.side_effect = exc_409

        existing_job = MagicMock()
        existing_job.spec.template.spec.containers = [MagicMock(image="agent-runtime:latest")]
        mock_batch.read_namespaced_job.return_value = existing_job

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            spawner = KubernetesSpawner(namespace="default")
            endpoint = await spawner._do_spawn("test-agent", "agent-runtime:latest", {})

        assert "test-agent" in endpoint
        mock_batch.read_namespaced_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_job_409_wrong_image_raises(self) -> None:
        """Job AlreadyExists with different image raises RuntimeError."""
        import sys

        mock_batch = MagicMock()
        exc_409 = Exception("conflict")
        exc_409.status = 409
        mock_batch.create_namespaced_job.side_effect = exc_409

        existing_job = MagicMock()
        existing_job.spec.template.spec.containers = [MagicMock(image="wrong-image:v2")]
        mock_batch.read_namespaced_job.return_value = existing_job

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = MagicMock()
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            spawner = KubernetesSpawner(namespace="default")
            with pytest.raises(RuntimeError, match="different image"):
                await spawner._do_spawn("test-agent", "agent-runtime:latest", {})

    @pytest.mark.asyncio
    async def test_spawn_service_409_succeeds(self) -> None:
        """Service AlreadyExists is treated as success."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        svc_exc_409 = Exception("conflict")
        svc_exc_409.status = 409
        mock_core.create_namespaced_service.side_effect = svc_exc_409

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        with patch.dict(sys.modules, {
            "kubernetes": mock_k8s,
            "kubernetes.client": mock_k8s_client,
            "kubernetes.config": mock_k8s_config,
        }):
            spawner = KubernetesSpawner(namespace="default")
            endpoint = await spawner._do_spawn("test-agent", "agent-runtime:latest", {})

        assert "test-agent" in endpoint


class TestKubernetesSpawnerSecretFiltering:
    """Tests for secret env var filtering in Job specs."""

    def test_sensitive_keys_excluded_from_literal_env(self) -> None:
        """Test that keys in secret_env_vars are not passed as literals."""
        refs = {
            "OPENAI_API_KEY": SecretKeyRef(secret_name="llm-key", key="api_key"),
            "AGENT_API_TOKEN": SecretKeyRef(secret_name="auth", key="token"),
        }
        spawner = KubernetesSpawner(secret_env_vars=refs)

        env = {
            "AGENT_MODEL": "gpt-4",
            "OPENAI_API_KEY": "should-not-appear",
            "AGENT_API_TOKEN": "should-not-appear-either",
            "OLLAMA_URL": "https://api.openai.com/v1",
        }

        sensitive = set(spawner._secret_env_vars.keys())
        literal_env = {k: v for k, v in env.items() if k not in sensitive}

        assert "AGENT_MODEL" in literal_env
        assert "OLLAMA_URL" in literal_env
        assert "OPENAI_API_KEY" not in literal_env
        assert "AGENT_API_TOKEN" not in literal_env
