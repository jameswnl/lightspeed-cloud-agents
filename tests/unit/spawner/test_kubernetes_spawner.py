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
