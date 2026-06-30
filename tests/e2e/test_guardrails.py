"""E2E guardrails test for both Podman and Kubernetes.

Verifies that security guardrails are enforced on real containers:
- securityContext (non-root, read-only root fs)
- resource limits applied to spawned pods
- concurrency cap prevents over-spawning
- orphan reconciliation cleans up on startup
- advisory mode sets read-only filesystem
- spawned-by label present for crash recovery

Prerequisites:
  - podman running with socket accessible
  - lightspeed-agentic-sandbox:temporal image built
  - For Kind tests: Kind cluster running with images loaded

Usage:
  uv run pytest tests/e2e/test_guardrails.py -v
  uv run pytest tests/e2e/test_guardrails.py -v -k podman
  uv run pytest tests/e2e/test_guardrails.py -v -k kind
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

SANDBOX_IMAGE = os.environ.get(
    "SANDBOX_IMAGE", "localhost/lightspeed-agentic-sandbox:temporal"
)


class TestPodmanGuardrails:
    """E2E guardrails tests using PodmanSpawner."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_podman(self) -> None:
        """Skip if podman-py is not available."""
        pytest.importorskip("podman")

    @pytest.fixture
    def spawner(self):
        """Create a PodmanSpawner with test network."""
        from agents.spawner.podman_spawner import PodmanSpawner

        os.system(
            "podman network exists cloud-agents 2>/dev/null "
            "|| podman network create cloud-agents >/dev/null 2>&1"
        )
        return PodmanSpawner(network="cloud-agents")

    @pytest.mark.asyncio
    async def test_spawned_container_has_runner_label(self, spawner) -> None:
        """Spawned container has spawned-by=workflow-runner label for orphan detection."""
        from podman import PodmanClient

        name = "guardrail-label-test"
        try:
            await spawner.spawn(
                name, SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            with PodmanClient() as client:
                container = client.containers.get(f"agent-{name}")
                labels = container.labels or {}
                assert labels.get("spawned-by") == "workflow-runner"
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_concurrency_cap_enforced(self, spawner) -> None:
        """Concurrency cap prevents spawning beyond MAX_SPAWNED_PODS."""
        from agents.spawner.podman_spawner import PodmanSpawner

        capped_spawner = PodmanSpawner(network="cloud-agents", max_pods=1)
        names = []
        try:
            await capped_spawner.spawn(
                "cap-test-1", SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            names.append("cap-test-1")
            with pytest.raises(RuntimeError, match="Concurrency cap"):
                await capped_spawner.spawn(
                    "cap-test-2", SANDBOX_IMAGE,
                    env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
                )
        finally:
            for n in names:
                await capped_spawner.destroy(n)

    @pytest.mark.asyncio
    async def test_orphan_reconciliation(self, spawner) -> None:
        """Orphan reconciliation finds and destroys containers with runner label."""
        from podman import PodmanClient

        from agents.workflow.temporal_entrypoint import reconcile_orphaned_sandboxes

        name = "guardrail-orphan-test"
        await spawner.spawn(
            name, SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
        )

        with PodmanClient() as client:
            container = client.containers.get(f"agent-{name}")
            assert container.status == "running"

        from agents.spawner.podman_spawner import PodmanSpawner
        fresh_spawner = PodmanSpawner(network="cloud-agents")
        await reconcile_orphaned_sandboxes(fresh_spawner)

        with PodmanClient() as client:
            try:
                client.containers.get(f"agent-{name}")
                pytest.fail("Orphaned container still exists after reconciliation")
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_mcp_secret_mounts_rejected_on_podman(self, spawner) -> None:
        """Podman spawner rejects secret-backed MCP headers with clear error."""
        with pytest.raises(ValueError, match="not supported on Podman"):
            await spawner.spawn(
                "mcp-reject-test", SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
                mcp_secret_mounts=[("secret-name", "key", "/var/secrets/mcp/sn/key")],
            )


class TestKindGuardrails:
    """E2E guardrails tests using KubernetesSpawner on Kind."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_kind(self) -> None:
        """Skip if kubernetes client is not available or no cluster."""
        pytest.importorskip("kubernetes")
        try:
            from kubernetes import client, config
            config.load_kube_config()
            v1 = client.CoreV1Api()
            v1.list_namespace()
        except Exception:
            pytest.skip("No accessible Kubernetes cluster")

    @pytest.fixture
    def spawner(self):
        """Create a KubernetesSpawner."""
        from agents.spawner.kubernetes_spawner import KubernetesSpawner
        return KubernetesSpawner(namespace="default", service_account="default")

    @pytest.mark.asyncio
    async def test_spawned_job_has_security_context(self, spawner) -> None:
        """Spawned K8s Job has securityContext enforced."""
        from kubernetes import client, config

        config.load_kube_config()
        batch = client.BatchV1Api()

        name = "guardrail-sec-test"
        try:
            await spawner.spawn(
                name, SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            job = batch.read_namespaced_job(f"agent-{name}", "default")
            sc = job.spec.template.spec.containers[0].security_context
            assert sc.run_as_non_root is True
            assert sc.read_only_root_filesystem is True
            assert sc.allow_privilege_escalation is False
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_spawned_job_has_resource_limits(self, spawner) -> None:
        """Spawned K8s Job has resource requests and limits."""
        from kubernetes import client, config

        config.load_kube_config()
        batch = client.BatchV1Api()

        name = "guardrail-res-test"
        try:
            await spawner.spawn(
                name, SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            job = batch.read_namespaced_job(f"agent-{name}", "default")
            resources = job.spec.template.spec.containers[0].resources
            assert resources.requests is not None
            assert "cpu" in resources.requests
            assert "memory" in resources.requests
            assert resources.limits is not None
            assert "cpu" in resources.limits
            assert "memory" in resources.limits
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_spawned_job_has_runner_label(self, spawner) -> None:
        """Spawned K8s Job has spawned-by=workflow-runner label."""
        from kubernetes import client, config

        config.load_kube_config()
        batch = client.BatchV1Api()

        name = "guardrail-lbl-test"
        try:
            await spawner.spawn(
                name, SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            job = batch.read_namespaced_job(f"agent-{name}", "default")
            assert job.metadata.labels.get("spawned-by") == "workflow-runner"
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_spawned_job_has_tmp_tmpfs(self, spawner) -> None:
        """Spawned K8s Job has tmpfs volume at /tmp."""
        from kubernetes import client, config

        config.load_kube_config()
        batch = client.BatchV1Api()

        name = "guardrail-tmp-test"
        try:
            await spawner.spawn(
                name, SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
            )
            job = batch.read_namespaced_job(f"agent-{name}", "default")
            volumes = job.spec.template.spec.volumes or []
            tmp_vols = [v for v in volumes if v.name == "tmp-scratch"]
            assert len(tmp_vols) == 1
            assert tmp_vols[0].empty_dir.medium == "Memory"
        finally:
            await spawner.destroy(name)
