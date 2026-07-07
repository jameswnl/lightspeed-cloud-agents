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
        patch("cloud_agents.spawner.kubernetes_spawner.KubernetesSpawner._do_spawn") as mock_spawn,
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


class TestKubernetesSpawnerWriteFile:
    """Tests for KubernetesSpawner._do_write_file() via kubectl exec."""

    @pytest.mark.asyncio
    async def test_write_file_calls_kubectl_exec(self) -> None:
        """write_file uses kubectl exec with stdin piping."""
        import subprocess

        spawner = KubernetesSpawner(namespace="test-ns")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await spawner._do_write_file("my-agent", "/tmp/test.txt", "hello")

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "kubectl" in cmd
            assert "exec" in cmd
            assert "-i" in cmd
            assert "agent-my-agent" in cmd
            assert "-n" in cmd
            assert "test-ns" in cmd
            assert call_args[1]["input"] == b"hello"
            assert call_args[1]["check"] is True

    @pytest.mark.asyncio
    async def test_write_file_raises_on_failure(self) -> None:
        """write_file raises RuntimeError when kubectl exec fails."""
        import subprocess

        spawner = KubernetesSpawner(namespace="test-ns")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "kubectl", stderr="permission denied"
            )
            with pytest.raises(RuntimeError, match="Failed to write"):
                await spawner._do_write_file("my-agent", "/tmp/test.txt", "content")


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
        tmp_vol_calls = [c for c in volume_calls if (c[1] or {}).get("name") == "tmp-scratch"]
        assert len(tmp_vol_calls) == 1, "Expected one V1Volume named 'tmp-scratch'"

        empty_dir_call = mock_k8s_client.V1EmptyDirVolumeSource.call_args_list
        mem_calls = [c for c in empty_dir_call if (c[1] or {}).get("medium") == "Memory"]
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
        cred_vol_calls = [c for c in volume_calls if (c[1] or {}).get("name") == "llm-credentials"]
        assert len(cred_vol_calls) == 1, "Expected one V1Volume named 'llm-credentials'"

        secret_vol_src_calls = mock_k8s_client.V1SecretVolumeSource.call_args_list
        cred_src_calls = [
            c for c in secret_vol_src_calls if (c[1] or {}).get("secret_name") == "llm-creds"
        ]
        assert len(cred_src_calls) == 1, "Expected V1SecretVolumeSource(secret_name='llm-creds')"

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
        assert len(env_from_calls) == 1, "Expected one V1EnvFromSource for credential secret"

        secret_env_calls = mock_k8s_client.V1SecretEnvSource.call_args_list
        cred_env_calls = [c for c in secret_env_calls if (c[1] or {}).get("name") == "llm-creds"]
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
        cred_vol_calls = [c for c in volume_calls if (c[1] or {}).get("name") == "llm-credentials"]
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


class TestKubernetesSpawnerTLS:
    """Tests for TLS cert injection in KubernetesSpawner."""

    def _make_tls_certs(self) -> "EphemeralCerts":
        """Create a mock EphemeralCerts for testing."""
        from cloud_agents.workflow.tls import EphemeralCerts

        return EphemeralCerts(
            ca_cert_pem=b"-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
            server_cert_pem=b"-----BEGIN CERTIFICATE-----\nSERVER\n-----END CERTIFICATE-----\n",
            server_key_pem=b"mock-server-key-pem-data",
            valid_seconds=600,
        )

    def _make_mock_k8s(self) -> tuple:
        """Create mock K8s client modules."""
        mock_batch = MagicMock()
        mock_core = MagicMock()

        mock_k8s_client = MagicMock()
        mock_k8s_client.BatchV1Api.return_value = mock_batch
        mock_k8s_client.CoreV1Api.return_value = mock_core
        mock_k8s_config = MagicMock()

        mock_k8s = MagicMock()
        mock_k8s.client = mock_k8s_client
        mock_k8s.config = mock_k8s_config

        return mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core

    @pytest.mark.asyncio
    async def test_tls_certs_creates_k8s_secret(self) -> None:
        """TLS certs provided -> K8s Secret created with cert+key data."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()
        tls_certs = self._make_tls_certs()

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
                "tls-agent",
                "agent-runtime:latest",
                {},
                tls_certs=tls_certs,
            )

        mock_core.create_namespaced_secret.assert_called_once()
        secret_call = mock_core.create_namespaced_secret.call_args
        assert secret_call[1]["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_tls_certs_adds_volume_mount(self) -> None:
        """TLS certs provided -> sandbox-tls volume mount added to pod spec."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()
        tls_certs = self._make_tls_certs()

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
                "tls-agent",
                "agent-runtime:latest",
                {},
                tls_certs=tls_certs,
            )

        volume_calls = mock_k8s_client.V1Volume.call_args_list
        tls_vol_calls = [c for c in volume_calls if (c[1] or {}).get("name") == "sandbox-tls"]
        assert len(tls_vol_calls) == 1

        mount_calls = mock_k8s_client.V1VolumeMount.call_args_list
        tls_mount_calls = [
            c
            for c in mount_calls
            if (c[1] or {}).get("name") == "sandbox-tls"
            and (c[1] or {}).get("mount_path") == "/var/run/secrets/sandbox-tls/"
        ]
        assert len(tls_mount_calls) == 1

    @pytest.mark.asyncio
    async def test_tls_certs_changes_port_to_8443(self) -> None:
        """TLS certs provided -> container port changed to 8443."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()
        tls_certs = self._make_tls_certs()

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
                "tls-agent",
                "agent-runtime:latest",
                {},
                tls_certs=tls_certs,
            )

        port_calls = mock_k8s_client.V1ContainerPort.call_args_list
        assert any(
            (c[1] or {}).get("container_port") == 8443 for c in port_calls
        ), "Expected container_port=8443 with TLS"

    @pytest.mark.asyncio
    async def test_tls_certs_endpoint_uses_https(self) -> None:
        """TLS certs provided -> endpoint URL uses https and port 8443."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()
        tls_certs = self._make_tls_certs()

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="default")
            endpoint = await spawner._do_spawn(
                "tls-agent",
                "agent-runtime:latest",
                {},
                tls_certs=tls_certs,
            )

        assert endpoint.startswith("https://")
        assert ":8443" in endpoint

    @pytest.mark.asyncio
    async def test_tls_certs_sets_env_vars(self) -> None:
        """TLS certs provided -> SANDBOX_TLS_CERT_PATH and _KEY_PATH env vars set."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()
        tls_certs = self._make_tls_certs()

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
                "tls-agent",
                "agent-runtime:latest",
                {},
                tls_certs=tls_certs,
            )

        env_calls = mock_k8s_client.V1EnvVar.call_args_list
        cert_path_calls = [
            c for c in env_calls if (c[1] or {}).get("name") == "SANDBOX_TLS_CERT_PATH"
        ]
        key_path_calls = [
            c for c in env_calls if (c[1] or {}).get("name") == "SANDBOX_TLS_KEY_PATH"
        ]
        assert len(cert_path_calls) == 1
        assert len(key_path_calls) == 1
        assert cert_path_calls[0][1]["value"] == "/var/run/secrets/sandbox-tls/tls.crt"
        assert key_path_calls[0][1]["value"] == "/var/run/secrets/sandbox-tls/tls.key"

    @pytest.mark.asyncio
    async def test_no_tls_certs_no_secret_created(self) -> None:
        """tls_certs=None -> no K8s Secret created, endpoint uses http."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="default")
            endpoint = await spawner._do_spawn(
                "no-tls-agent",
                "agent-runtime:latest",
                {},
            )

        mock_core.create_namespaced_secret.assert_not_called()
        assert endpoint.startswith("http://")
        assert ":8080" in endpoint

    @pytest.mark.asyncio
    async def test_destroy_deletes_tls_secret(self) -> None:
        """Destroy deletes the TLS Secret for the agent."""
        import sys

        mock_k8s, mock_k8s_client, mock_k8s_config, mock_batch, mock_core = self._make_mock_k8s()

        with patch.dict(
            sys.modules,
            {
                "kubernetes": mock_k8s,
                "kubernetes.client": mock_k8s_client,
                "kubernetes.config": mock_k8s_config,
            },
        ):
            spawner = KubernetesSpawner(namespace="default")
            await spawner._do_destroy("tls-agent")

        # Verify best-effort TLS Secret cleanup was attempted
        secret_delete_calls = mock_core.delete_namespaced_secret.call_args_list
        tls_secret_calls = [
            c for c in secret_delete_calls if (c[1] or {}).get("name") == "sandbox-tls-tls-agent"
        ]
        assert len(tls_secret_calls) == 1


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
            c for c in volume_calls if (c[1] or {}).get("name") == "mcp-secret-mcp-sn-token"
        ]
        assert len(mcp_vol_calls) == 1, "Expected one V1Volume named 'mcp-secret-mcp-sn-token'"

        # Verify SecretVolumeSource points to correct secret
        secret_vol_src_calls = mock_k8s_client.V1SecretVolumeSource.call_args_list
        mcp_src_calls = [
            c for c in secret_vol_src_calls if (c[1] or {}).get("secret_name") == "mcp-sn-token"
        ]
        assert len(mcp_src_calls) == 1, "Expected V1SecretVolumeSource(secret_name='mcp-sn-token')"

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
            c for c in volume_calls if (c[1] or {}).get("name", "").startswith("mcp-secret-")
        ]
        assert (
            len(mcp_vol_calls) == 0
        ), "Expected no mcp-secret-* volumes when mcp_secret_mounts is None"
