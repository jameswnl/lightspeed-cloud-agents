"""E2E guardrails test for Podman, Kubernetes, and OpenShell.

Verifies that security guardrails are enforced on real containers:
- securityContext (non-root, read-only root fs)
- resource limits applied to spawned pods
- concurrency cap prevents over-spawning
- orphan reconciliation cleans up on startup
- advisory mode sets read-only filesystem
- spawned-by label present for crash recovery
- OpenShell non-advisory spawns can read their own image contents
  (Landlock filesystem policy -- issue #189)

Prerequisites:
  - podman running with socket accessible
  - lightspeed-agentic-sandbox:temporal image built
  - For Kind tests: Kind cluster running with images loaded
  - For OpenShell tests: a reachable OpenShell gateway (see
    OPENSHELL_GATEWAY_URL / OPENSHELL_SANDBOX_IMAGE below)

Usage:
  uv run pytest tests/e2e/test_guardrails.py -v
  uv run pytest tests/e2e/test_guardrails.py -v -k podman
  uv run pytest tests/e2e/test_guardrails.py -v -k kind
  uv run pytest tests/e2e/test_guardrails.py -v -k openshell
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from typing import Any

import httpx
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
        from cloud_agents.spawner.podman_spawner import PodmanSpawner

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
        from cloud_agents.spawner.podman_spawner import PodmanSpawner

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

        from cloud_agents.workflow.executor.temporal.entrypoint import reconcile_orphaned_sandboxes

        name = "guardrail-orphan-test"
        await spawner.spawn(
            name, SANDBOX_IMAGE,
            env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
        )

        with PodmanClient() as client:
            container = client.containers.get(f"agent-{name}")
            assert container.status == "running"

        from cloud_agents.spawner.podman_spawner import PodmanSpawner
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
        from cloud_agents.spawner.kubernetes_spawner import KubernetesSpawner
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


OPENSHELL_GATEWAY_URL = os.environ.get("OPENSHELL_GATEWAY_URL", "localhost:9080")
OPENSHELL_SANDBOX_IMAGE = os.environ.get(
    "OPENSHELL_SANDBOX_IMAGE", "quay.io/jameswong/lightspeed-agentic-sandbox:latest"
)
# The production Containerfile image (issue #192) -- distinct from
# OPENSHELL_SANDBOX_IMAGE above because that default (:latest, built from
# Containerfile.dev) installs its Python packages under /usr/local,
# already in OpenShell's own default allowlist, so it never needed
# PYTHONPATH and would not catch this regression. The production
# Containerfile installs lightspeed_agentic at /opt/lightspeed/src
# instead, which does. The published tag is architecture-specific (no
# multi-arch manifest), so the default is derived from the host's own
# architecture rather than hardcoded -- override via env var if the
# gateway's compute node doesn't match the host running pytest.
_ARCH_TAG = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
    platform.machine()
)
OPENSHELL_PYTHONPATH_TEST_IMAGE = os.environ.get(
    "OPENSHELL_PYTHONPATH_TEST_IMAGE",
    f"quay.io/jameswong/lightspeed-agentic-sandbox:latest-{_ARCH_TAG}"
    if _ARCH_TAG
    else None,
)


async def _exec(client: Any, sandbox_id: str, command: list[str]) -> Any:
    """Run a blocking exec_stream() call off the event loop and return the ExecResult.

    OpenShell's SandboxClient.exec_stream() is a synchronous generator, so
    it must be driven from a worker thread (same pattern OpenShellSpawner
    itself uses internally for start_server()/_consume_exec()).
    """

    def _sync() -> Any:
        result = None
        for item in client.exec_stream(sandbox_id, command):
            if hasattr(item, "exit_code"):
                result = item
        return result

    return await asyncio.to_thread(_sync)


class TestOpenShellGuardrails:
    """E2E guardrail test for OpenShellSpawner against a REAL gateway (issue #189).

    Regression coverage for the Landlock filesystem-policy gap: unit tests
    can only assert the constructed policy's *shape* (see
    tests/unit/spawner/test_openshell_spawner.py::TestBaselineFilesystemPolicy)
    -- they cannot catch the actual bug, which is about what OpenShell's
    real gateway/supervisor does with an empty filesystem-policy field,
    something no mock reproduces. This class is the real gate.

    A bare `exec(["echo", "hello"])` would pass today without ever
    touching a Landlock-restricted path, and would not catch this bug --
    so these tests deliberately probe real filesystem access instead.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_gateway(self) -> None:
        """Skip (rather than fail) when no real OpenShell gateway is reachable."""
        pytest.importorskip("openshell")
        try:
            httpx.get(f"http://{OPENSHELL_GATEWAY_URL}/", timeout=3.0)
        except httpx.HTTPError:
            pytest.skip(f"No accessible OpenShell gateway at {OPENSHELL_GATEWAY_URL}")

    @pytest.fixture
    def spawner(self):
        """Build a real OpenShellSpawner against the configured gateway."""
        from cloud_agents.spawner.factory import build_spawner

        return build_spawner(
            "openshell",
            gateway_url=OPENSHELL_GATEWAY_URL,
            driver="podman",
            workspace="default",
        )

    @pytest.mark.asyncio
    async def test_non_advisory_spawn_can_import_its_own_dependencies(self, spawner) -> None:
        """A normal (non-advisory) ephemeral spawn must be able to read its own image.

        The reference sandbox image installs its Python packages under
        /usr/local (already in OpenShell's own hardcoded default
        allowlist), so this test alone does not reproduce the exact
        /opt/app-root path from issue #189 against the currently
        published image tag -- see
        test_extra_readable_paths_widens_landlock_access_on_real_gateway
        below for the test that exercises the actual policy-composition
        mechanism the fix depends on. This test is still valuable as a
        real-world integration check: it proves the new baseline
        filesystem policy doesn't regress normal, non-advisory server
        startup. OpenShellSpawner._do_spawn() internally waits for
        /health to return 200 (_wait_ready_with_host) and raises
        RuntimeError if it times out -- so spawn() completing without
        raising is already proof the server started and uvicorn imported
        cleanly. This test additionally re-confirms /health directly.
        """
        name = "landlock-baseline-e2e-test"
        try:
            endpoint = await spawner.spawn(
                name,
                OPENSHELL_SANDBOX_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
                read_only=False,
            )
            assert endpoint

            headers = spawner.get_sandbox_headers(name)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{endpoint}/health", headers=headers)
            assert resp.status_code == 200
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_extra_readable_paths_widens_landlock_access_on_real_gateway(self) -> None:
        """The core regression test for issue #189, against a REAL gateway.

        /home is outside OpenShell's own hardcoded default allowlist
        (/usr, /lib, /proc, /dev/urandom, /app, /etc, /var/log) and
        outside this reference image's own package layout, so it is a
        stable stand-in for "a path the caller's image needs that the
        gateway's default doesn't grant" -- exactly the shape of the
        original /opt/app-root bug, without depending on where any one
        image build happens to install its packages.

        Without `/home` in `extra_readable_paths`, a non-advisory spawn
        must still be denied (`ls /home` -> Permission denied) -- proving
        Landlock enforcement is genuinely active in this environment, not
        silently skipped. With `/home` added to `extra_readable_paths`,
        the same command must succeed -- proving the baseline builder's
        default-allowlist-union-extras is what actually reaches the real
        gateway's enforcement, not just a shape asserted against a mock.
        """
        from cloud_agents.spawner.factory import build_spawner

        denied_spawner = build_spawner(
            "openshell",
            gateway_url=OPENSHELL_GATEWAY_URL,
            driver="podman",
            workspace="default",
            extra_readable_paths=[],
        )
        allowed_spawner = build_spawner(
            "openshell",
            gateway_url=OPENSHELL_GATEWAY_URL,
            driver="podman",
            workspace="default",
            extra_readable_paths=["/home"],
        )
        env = {"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"}

        denied_name = "landlock-extra-denied-test"
        try:
            await denied_spawner.spawn(
                denied_name, OPENSHELL_SANDBOX_IMAGE, env=env, read_only=False
            )
            denied_id = denied_spawner.get_sandbox_id(denied_name)
            denied_result = await _exec(denied_spawner._client, denied_id, ["ls", "/home"])
            assert denied_result is not None
            assert denied_result.exit_code != 0
            # Assert the specific reason, not just a nonzero exit -- ENOENT or a
            # broken exec would also be nonzero without proving Landlock denial.
            assert "permission denied" in denied_result.stderr.lower()
        finally:
            await denied_spawner.destroy(denied_name)

        allowed_name = "landlock-extra-allowed-test"
        try:
            await allowed_spawner.spawn(
                allowed_name, OPENSHELL_SANDBOX_IMAGE, env=env, read_only=False
            )
            allowed_id = allowed_spawner.get_sandbox_id(allowed_name)
            allowed_result = await _exec(allowed_spawner._client, allowed_id, ["ls", "/home"])
            assert allowed_result is not None
            assert allowed_result.exit_code == 0
        finally:
            await allowed_spawner.destroy(allowed_name)

    @pytest.mark.asyncio
    async def test_production_image_server_becomes_ready_with_pythonpath(self) -> None:
        """A non-advisory spawn of the production-layout image reaches a healthy server (issue #192).

        OPENSHELL_SANDBOX_IMAGE (:latest, Containerfile.dev) installs its
        Python packages under /usr/local -- already in OpenShell's own
        default exec allowlist -- so a spawn against it never needed
        PYTHONPATH and would not catch this bug (see
        test_non_advisory_spawn_can_import_its_own_dependencies above,
        which has the same caveat for the analogous #189 case). The
        production Containerfile installs lightspeed_agentic at
        /opt/lightspeed/src instead: OpenShell's supervisor calls
        env_clear() before exec'ing start_server()'s command
        (apply_child_env(), ssh.rs) and rebuilds the environment from a
        hardcoded allowlist that does not include PYTHONPATH, so without
        OpenShellSpawner explicitly injecting it, `python3 -m uvicorn
        lightspeed_agentic.app:app` fails with ModuleNotFoundError and
        spawn() raises "HTTP server did not become ready" -- reproduced
        directly against a real gateway while implementing this fix.
        """
        if OPENSHELL_PYTHONPATH_TEST_IMAGE is None:
            pytest.skip(
                f"Unrecognized host architecture {platform.machine()!r} -- no published "
                "image tag to default to. Set OPENSHELL_PYTHONPATH_TEST_IMAGE explicitly."
            )

        from cloud_agents.spawner.factory import build_spawner

        spawner = build_spawner(
            "openshell",
            gateway_url=OPENSHELL_GATEWAY_URL,
            driver="podman",
            workspace="default",
        )

        name = "pythonpath-e2e-test"
        try:
            endpoint = await spawner.spawn(
                name,
                OPENSHELL_PYTHONPATH_TEST_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
                read_only=False,
            )
            assert endpoint

            headers = spawner.get_sandbox_headers(name)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{endpoint}/health", headers=headers)
            assert resp.status_code == 200
        finally:
            await spawner.destroy(name)
