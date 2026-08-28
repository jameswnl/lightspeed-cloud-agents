"""E2E guardrails test for OpenShell.

Verifies that security guardrails are enforced on real containers:
- advisory mode sets read-only filesystem
- OpenShell non-advisory spawns can read their own image contents
  (Landlock filesystem policy -- issue #189)

OpenShellSpawner is the only supported ephemeral spawner (issue #198);
PodmanSpawner/KubernetesSpawner-specific guardrail tests were removed
along with those classes.

Prerequisites:
  - a reachable OpenShell gateway (see OPENSHELL_GATEWAY_URL /
    OPENSHELL_SANDBOX_IMAGE below)

Usage:
  uv run pytest tests/e2e/test_guardrails.py -v
  uv run pytest tests/e2e/test_guardrails.py -v -k openshell
"""

from __future__ import annotations

import asyncio
import os
import platform
from typing import Any

import httpx
import pytest

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
    f"quay.io/jameswong/lightspeed-agentic-sandbox:latest-{_ARCH_TAG}" if _ARCH_TAG else None,
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
            workspace="default",
            extra_readable_paths=[],
        )
        allowed_spawner = build_spawner(
            "openshell",
            gateway_url=OPENSHELL_GATEWAY_URL,
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

    @pytest.mark.asyncio
    async def test_allowed_skills_scoping_on_real_gateway(self) -> None:
        """allowed_skills scoping actually works end-to-end (issue #202), against a REAL gateway.

        Unit tests can only assert the Landlock policy's *shape* and that
        exec_stream() was called with the right argv -- they cannot catch
        the bug a human reviewer found in the first version of this PR:
        Landlock's allow-list model can't grant partial directory listing
        of /skills, so a naive per-name-grant-only design leaves agent
        providers (which discover skills by *listing*
        LIGHTSPEED_SKILLS_DIR) either unable to list anything or able to
        list every baked-in skill regardless of allowed_skills. This test
        exercises the real fix -- OpenShellSpawner execing the sandbox
        image's baked-in materialize-skills.sh before starting the agent
        server -- against a real gateway and a real sandbox image built
        from lightspeed-agentic-sandbox's Containerfile.

        Requires OPENSHELL_PYTHONPATH_TEST_IMAGE (or its arch-derived
        default) to be built from a lightspeed-agentic-sandbox revision
        that includes materialize-skills.sh and the skills/ directory
        (see that repo's PR #9) -- older image tags predate both and will
        fail this test with ENOENT on materialize-skills.sh.
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
            workspace="default",
        )

        name = "allowed-skills-e2e-test"
        try:
            endpoint = await spawner.spawn(
                name,
                OPENSHELL_PYTHONPATH_TEST_IMAGE,
                env={"LIGHTSPEED_PROVIDER": "openai", "LIGHTSPEED_MODEL": "gpt-4o-mini"},
                read_only=False,
                allowed_skills=["k8s-diag"],
            )
            assert endpoint

            sandbox_id = spawner.get_sandbox_id(name)

            # (1) The allowed skill is readable through the real Landlock
            # grant -- the actual enforcement boundary, not just the copy.
            read_allowed = await _exec(
                spawner._client, sandbox_id, ["cat", "/skills/k8s-diag/SKILL.md"]
            )
            assert read_allowed is not None
            assert read_allowed.exit_code == 0

            # (2) An unlisted skill is genuinely unreadable at its master
            # location, not merely absent from the materialized copy --
            # proving Landlock is doing real work here, not just the script.
            read_denied = await _exec(
                spawner._client, sandbox_id, ["cat", "/skills/git-ops/SKILL.md"]
            )
            assert read_denied is not None
            assert read_denied.exit_code != 0

            # (3) materialize-skills.sh copied only the allowed name into
            # the directory providers actually list (LIGHTSPEED_SKILLS_DIR).
            listing = await _exec(spawner._client, sandbox_id, ["ls", "/app/skills"])
            assert listing is not None
            assert listing.exit_code == 0
            assert "k8s-diag" in listing.stdout
            assert "git-ops" not in listing.stdout
        finally:
            await spawner.destroy(name)


# Requires a REAL TLS-enabled OpenShell gateway with a CA that is NOT in
# the system trust store (e.g. cert-manager self-signed, matching a real
# deployment) -- the bug this guards against (issue #194) cannot
# reproduce without one: a plain HTTP gateway (the default used by every
# other test in this file) never exercises TLS verification at all, and
# a publicly-trusted CA would silently pass even with the bug present.
# Set OPENSHELL_TLS_CA to run this; skipped otherwise -- unlike the
# OPENSHELL_* defaults above, there's no sensible default here, since
# self-signed CAs are per-cluster.
OPENSHELL_TLS_CA = os.environ.get("OPENSHELL_TLS_CA")
OPENSHELL_TLS_GATEWAY_URL = os.environ.get("OPENSHELL_TLS_GATEWAY_URL", "localhost:9080")
OPENSHELL_TLS_BEARER_TOKEN = os.environ.get("OPENSHELL_TLS_BEARER_TOKEN")


class TestOpenShellQueryTLS:
    """E2E test for issue #194: query-time TLS trust for OpenShellSpawner.

    OpenShellSpawner._wait_ready_with_host() already builds a correct SSL
    context from the spawner's own tls_ca/tls_cert/tls_key -- that's why
    spawn()/wait_ready() succeed cleanly even against a self-signed
    gateway. But step_runner.py's *separate* query-call httpx client used
    to have no way to learn about that CA at all, and fell back to
    httpx's default system trust store, failing with
    CERTIFICATE_VERIFY_FAILED. A unit test mocking httpx can assert the
    client_kwargs shape, but can't prove real TLS verification actually
    succeeds against a real self-signed cert -- this is the real gate.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_tls_gateway(self) -> None:
        """Skip (rather than fail) when no TLS-enabled gateway is configured.

        Deliberately does NOT require OPENAI_API_KEY: this test verifies
        the query call's TLS handshake succeeds, which happens entirely
        at the transport layer before any LLM credential is used inside
        the sandbox. `status in ("completed", "failed")` already treats
        an agent-side failure (e.g. missing/invalid provider credentials)
        as proof the TLS layer worked -- requiring a real API key here
        would only make this regression gate skip more often than it
        needs to, for no added coverage.
        """
        pytest.importorskip("openshell")
        if not OPENSHELL_TLS_CA:
            pytest.skip(
                "OPENSHELL_TLS_CA not set -- no TLS-enabled OpenShell gateway configured "
                "(a plain HTTP gateway can't reproduce this bug at all)"
            )

    @pytest.mark.asyncio
    async def test_query_call_trusts_spawner_ca_end_to_end(self) -> None:
        """The full step_runner.run_step() path succeeds against a real TLS gateway.

        Before the fix: this raised
        `httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate
        verify failed: unable to get local issuer certificate`, from
        step_runner.py's query-call httpx client -- even though this
        exact spawner/gateway/CA combination already spawns and passes
        wait_ready() cleanly, since that path builds its own SSL context
        correctly. Confirmed live against a real OCP cluster with a
        cert-manager self-signed gateway CA before implementing the fix.
        """
        from cloud_agents.spawner.factory import build_spawner
        from cloud_agents.workflow.core.step_runner import run_step

        spawner = build_spawner(
            "openshell",
            gateway_url=OPENSHELL_TLS_GATEWAY_URL,
            workspace="default",
            tls_ca=OPENSHELL_TLS_CA,
            bearer_token=OPENSHELL_TLS_BEARER_TOKEN or "",
        )

        try:
            result = await run_step(
                input={
                    "step": {"name": "query-tls-e2e-test", "prompt": "Say hello in one word."},
                    "workflow_id": "e2e-query-tls",
                    "provider": {"name": "openai", "model": "gpt-4o-mini"},
                    "sandbox_image": OPENSHELL_SANDBOX_IMAGE,
                },
                spawner=spawner,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            # httpx.ConnectError is what actually propagates from run_step()
            # today (confirmed live, pre-fix) -- not caught/wrapped into a
            # RuntimeError anywhere on this path. Checking both exception
            # types (rather than just RuntimeError) is itself part of what
            # this test verifies: asserting only RuntimeError would silently
            # let a real ConnectError escape as an unhandled test error
            # instead of a clear, diagnosable failure message.
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                pytest.fail(f"Query call did not trust the spawner's own gateway CA: {exc}")
            raise

        # A real response (whether the agent's own answer succeeded or
        # failed) proves the query call completed a real TLS handshake --
        # the bug this guards against fails before any response exists.
        assert result["status"] in ("completed", "failed")
