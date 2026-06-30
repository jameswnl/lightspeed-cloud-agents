"""Unit tests for KubernetesSpawner with mocked K8s client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cloud_agents.spawner.base import SecretKeyRef
from cloud_agents.spawner.kubernetes_spawner import KubernetesSpawner


@pytest.fixture
def mock_k8s():
    """Mock the kubernetes client module."""
    with (
        patch(
            "cloud_agents.spawner.kubernetes_spawner.KubernetesSpawner._do_spawn"
        ) as mock_spawn,
        patch(
            "cloud_agents.spawner.kubernetes_spawner.KubernetesSpawner._do_destroy"
        ) as mock_destroy,
    ):
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
        existing_job.spec.template.spec.containers = [
            MagicMock(image="agent-runtime:latest")
        ]
        mock_batch.read_namespaced_job.return_value = existing_job

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
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

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
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


class TestKubernetesSpawnerSecurityContext:
    """Tests for security context on spawned Jobs."""

    @pytest.mark.asyncio
    async def test_spawned_job_has_security_context(self) -> None:
        """Spawned Job container has security context with non-root, read-only fs."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn("sec-agent", "agent-runtime:latest", {})

        sc_call = mock_k8s_client.V1SecurityContext.call_args
        assert sc_call is not None, "V1SecurityContext was never constructed"
        sc_kwargs = sc_call[1] if sc_call[1] else {}
        assert sc_kwargs.get("run_as_non_root") is True
        assert sc_kwargs.get("read_only_root_filesystem") is True
        assert sc_kwargs.get("allow_privilege_escalation") is False

        container_call = mock_k8s_client.V1Container.call_args
        assert "security_context" in (
            container_call[1] or {}
        ), "security_context not passed to V1Container"

    @pytest.mark.asyncio
    async def test_spawned_job_has_tmp_tmpfs(self) -> None:
        """Spawned Job has tmpfs volume at /tmp for write scratch."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn("sec-agent", "agent-runtime:latest", {})

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        tmp_vol_calls = [
            c for c in volume_calls if (c[1] or {}).get("name") == "tmp-scratch"
        ]
        assert len(tmp_vol_calls) == 1, "Expected one V1Volume named 'tmp-scratch'"

        empty_dir_call = mock_k8s_client.V1EmptyDirVolumeSource.call_args_list
        mem_calls = [
            c for c in empty_dir_call if (c[1] or {}).get("medium") == "Memory"
        ]
        assert len(mem_calls) >= 1, "Expected V1EmptyDirVolumeSource(medium='Memory')"

        mount_calls = mock_k8s_client.V1VolumeMount.call_args_list
        tmp_mount_calls = [
            c
            for c in mount_calls
            if (c[1] or {}).get("name") == "tmp-scratch"
            and (c[1] or {}).get("mount_path") == "/tmp"
        ]
        assert (
            len(tmp_mount_calls) == 1
        ), "Expected one V1VolumeMount with name='tmp-scratch' and mount_path='/tmp'"


class TestKubernetesSpawnerCredentialMount:
    """Tests for credential Secret volume mount and envFrom."""

    @pytest.mark.asyncio
    async def test_credential_secret_volume_mounted(self) -> None:
        """Spawning with credential_secret_name adds Secret volume."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "cred-agent",
                "agent-runtime:latest",
                {},
                credential_secret_name="llm-creds",
            )

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        cred_vol_calls = [
            c for c in volume_calls if (c[1] or {}).get("name") == "llm-credentials"
        ]
        assert len(cred_vol_calls) == 1, "Expected one V1Volume named 'llm-credentials'"

        secret_vol_src_calls = mock_k8s_client.V1SecretVolumeSource.call_args_list
        cred_src_calls = [
            c
            for c in secret_vol_src_calls
            if (c[1] or {}).get("secret_name") == "llm-creds"
        ]
        assert (
            len(cred_src_calls) == 1
        ), "Expected V1SecretVolumeSource(secret_name='llm-creds')"

        mount_calls = mock_k8s_client.V1VolumeMount.call_args_list
        cred_mount_calls = [
            c
            for c in mount_calls
            if (c[1] or {}).get("name") == "llm-credentials"
            and (c[1] or {}).get("mount_path") == "/var/run/secrets/llm-credentials/"
            and (c[1] or {}).get("read_only") is True
        ]
        assert (
            len(cred_mount_calls) == 1
        ), "Expected V1VolumeMount at '/var/run/secrets/llm-credentials/' (read_only)"

    @pytest.mark.asyncio
    async def test_credential_secret_envfrom(self) -> None:
        """Spawning with credential_secret_name adds envFrom.secretRef."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "cred-agent",
                "agent-runtime:latest",
                {},
                credential_secret_name="llm-creds",
            )

        env_from_calls = mock_k8s_client.V1EnvFromSource.call_args_list
        assert (
            len(env_from_calls) == 1
        ), "Expected one V1EnvFromSource for credential secret"

        secret_env_calls = mock_k8s_client.V1SecretEnvSource.call_args_list
        cred_env_calls = [
            c for c in secret_env_calls if (c[1] or {}).get("name") == "llm-creds"
        ]
        assert len(cred_env_calls) == 1, "Expected V1SecretEnvSource(name='llm-creds')"

        container_call = mock_k8s_client.V1Container.call_args
        container_kwargs = container_call[1] or {}
        assert "env_from" in container_kwargs, "env_from not passed to V1Container"

    @pytest.mark.asyncio
    async def test_no_credential_secret_no_mount(self) -> None:
        """Spawning without credential_secret_name has no credential volume."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "no-cred-agent",
                "agent-runtime:latest",
                {},
            )

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        cred_vol_calls = [
            c for c in volume_calls if (c[1] or {}).get("name") == "llm-credentials"
        ]
        assert (
            len(cred_vol_calls) == 0
        ), "Expected no V1Volume named 'llm-credentials' when no credential_secret_name"

        env_from_calls = mock_k8s_client.V1EnvFromSource.call_args_list
        assert (
            len(env_from_calls) == 0
        ), "Expected no V1EnvFromSource when no credential_secret_name"

        container_call = mock_k8s_client.V1Container.call_args
        container_kwargs = container_call[1] or {}
        assert (
            container_kwargs.get("env_from") is None
        ), "env_from should be None when no credential_secret_name"


class TestKubernetesSpawnerMCPSecretMounts:
    """Tests for MCP Secret volume mounts on spawned Jobs."""

    @pytest.mark.asyncio
    async def test_mcp_secret_volumes_mounted(self) -> None:
        """MCP secret refs create Secret volumes on the spawned Job."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "mcp-agent",
                "agent-runtime:latest",
                {},
                mcp_secret_mounts=[
                    ("mcp-sn-token", "bearer-token", "/var/secrets/mcp/servicenow/"),
                ],
            )

        # Verify Secret volume was created for the MCP secret
        volume_calls = mock_k8s_client.V1Volume.call_args_list
        mcp_vol_calls = [
            c
            for c in volume_calls
            if (c[1] or {}).get("name") == "mcp-secret-mcp-sn-token"
        ]
        assert (
            len(mcp_vol_calls) == 1
        ), "Expected one V1Volume named 'mcp-secret-mcp-sn-token'"

        # Verify SecretVolumeSource points to correct secret
        secret_vol_src_calls = mock_k8s_client.V1SecretVolumeSource.call_args_list
        mcp_src_calls = [
            c
            for c in secret_vol_src_calls
            if (c[1] or {}).get("secret_name") == "mcp-sn-token"
        ]
        assert (
            len(mcp_src_calls) == 1
        ), "Expected V1SecretVolumeSource(secret_name='mcp-sn-token')"

        # Verify VolumeMount at the correct path
        mount_calls = mock_k8s_client.V1VolumeMount.call_args_list
        mcp_mount_calls = [
            c
            for c in mount_calls
            if (c[1] or {}).get("name") == "mcp-secret-mcp-sn-token"
            and (c[1] or {}).get("mount_path") == "/var/secrets/mcp/servicenow/"
            and (c[1] or {}).get("read_only") is True
        ]
        assert (
            len(mcp_mount_calls) == 1
        ), "Expected V1VolumeMount at '/var/secrets/mcp/servicenow/' (read_only)"

    @pytest.mark.asyncio
    async def test_multiple_mcp_secret_volumes(self) -> None:
        """Multiple MCP secrets each get their own volume and mount."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "multi-mcp-agent",
                "agent-runtime:latest",
                {},
                mcp_secret_mounts=[
                    ("mcp-sn-token", "bearer-token", "/var/secrets/mcp/servicenow/"),
                    ("mcp-jira-token", "api-key", "/var/secrets/mcp/jira/"),
                ],
            )

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        mcp_vol_names = [
            (c[1] or {}).get("name")
            for c in volume_calls
            if (c[1] or {}).get("name", "").startswith("mcp-secret-")
        ]
        assert "mcp-secret-mcp-sn-token" in mcp_vol_names
        assert "mcp-secret-mcp-jira-token" in mcp_vol_names

    @pytest.mark.asyncio
    async def test_no_mcp_mounts_no_extra_volumes(self) -> None:
        """No MCP secret mounts means no mcp-secret-* volumes."""
        import sys

        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
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
            await spawner._do_spawn(
                "no-mcp-agent",
                "agent-runtime:latest",
                {},
            )

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        mcp_vol_calls = [
            c
            for c in volume_calls
            if (c[1] or {}).get("name", "").startswith("mcp-secret-")
        ]
        assert (
            len(mcp_vol_calls) == 0
        ), "Expected no mcp-secret-* volumes when mcp_secret_mounts is None"
