"""E2E test: sandbox bearer token authentication.

Validates that the runner-to-sandbox auth flow works end-to-end:
- Spawned containers receive AGENT_API_TOKEN env var
- Unauthenticated requests are rejected (401/403)
- Correctly authenticated requests are accepted
- Wrong tokens are rejected

Prerequisites:
  - Podman running with socket accessible
  - lightspeed-agentic-sandbox:temporal image built
  - SANDBOX_AUTH_ENABLED=true in environment

Usage:
  SANDBOX_AUTH_ENABLED=true AGENT_API_TOKEN=test-secret \
    uv run pytest tests/e2e/test_sandbox_auth.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

SANDBOX_IMAGE = os.environ.get(
    "SANDBOX_IMAGE", "localhost/lightspeed-agentic-sandbox:temporal"
)


@pytest.mark.skipif(
    not os.environ.get("SANDBOX_AUTH_ENABLED", "").lower() == "true",
    reason="Sandbox auth E2E tests require SANDBOX_AUTH_ENABLED=true",
)
class TestSandboxAuthE2E:
    """E2E tests for sandbox bearer token authentication.

    Spawns a real container with AGENT_API_TOKEN injected,
    then validates auth enforcement by making HTTP calls.
    """

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
    async def test_unauthenticated_rejected(self, spawner) -> None:
        """Request without Authorization header is rejected by sandbox."""
        import httpx

        auth_token = os.environ.get("AGENT_API_TOKEN", "test-auth-token")
        name = "auth-e2e-noauth"
        try:
            endpoint = await spawner.spawn(
                name,
                SANDBOX_IMAGE,
                env={
                    "LIGHTSPEED_PROVIDER": "openai",
                    "LIGHTSPEED_MODEL": "gpt-4o-mini",
                    "AGENT_API_TOKEN": auth_token,
                },
            )
            ready = await spawner.wait_ready(endpoint, health_path="/health")
            assert ready, "Sandbox never became ready"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"http://{endpoint}/v1/agent/run",
                    json={"query": "hello", "context": {}},
                )
                assert resp.status_code in (401, 403), (
                    f"Expected 401/403 without auth, got {resp.status_code}"
                )
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_correct_token_accepted(self, spawner) -> None:
        """Request with correct Bearer token is accepted by sandbox."""
        import httpx

        auth_token = os.environ.get("AGENT_API_TOKEN", "test-auth-token")
        name = "auth-e2e-correct"
        try:
            endpoint = await spawner.spawn(
                name,
                SANDBOX_IMAGE,
                env={
                    "LIGHTSPEED_PROVIDER": "openai",
                    "LIGHTSPEED_MODEL": "gpt-4o-mini",
                    "AGENT_API_TOKEN": auth_token,
                },
            )
            ready = await spawner.wait_ready(endpoint, health_path="/health")
            assert ready, "Sandbox never became ready"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"http://{endpoint}/v1/agent/run",
                    json={"query": "hello", "context": {}},
                    headers={"Authorization": f"Bearer {auth_token}"},
                )
                # Should not be 401/403 (may be 200 or 502 depending on
                # LLM provider availability, but auth passed)
                assert resp.status_code not in (401, 403), (
                    f"Expected auth to pass, got {resp.status_code}"
                )
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, spawner) -> None:
        """Request with wrong Bearer token is rejected by sandbox."""
        import httpx

        auth_token = os.environ.get("AGENT_API_TOKEN", "test-auth-token")
        name = "auth-e2e-wrong"
        try:
            endpoint = await spawner.spawn(
                name,
                SANDBOX_IMAGE,
                env={
                    "LIGHTSPEED_PROVIDER": "openai",
                    "LIGHTSPEED_MODEL": "gpt-4o-mini",
                    "AGENT_API_TOKEN": auth_token,
                },
            )
            ready = await spawner.wait_ready(endpoint, health_path="/health")
            assert ready, "Sandbox never became ready"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"http://{endpoint}/v1/agent/run",
                    json={"query": "hello", "context": {}},
                    headers={"Authorization": "Bearer wrong-token-value"},
                )
                assert resp.status_code in (401, 403), (
                    f"Expected 401/403 with wrong token, got {resp.status_code}"
                )
        finally:
            await spawner.destroy(name)

    @pytest.mark.asyncio
    async def test_health_endpoint_unauthenticated(self, spawner) -> None:
        """Health endpoint remains accessible without authentication."""
        import httpx

        auth_token = os.environ.get("AGENT_API_TOKEN", "test-auth-token")
        name = "auth-e2e-health"
        try:
            endpoint = await spawner.spawn(
                name,
                SANDBOX_IMAGE,
                env={
                    "LIGHTSPEED_PROVIDER": "openai",
                    "LIGHTSPEED_MODEL": "gpt-4o-mini",
                    "AGENT_API_TOKEN": auth_token,
                },
            )
            ready = await spawner.wait_ready(endpoint, health_path="/health")
            assert ready, "Sandbox never became ready"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"http://{endpoint}/health")
                assert resp.status_code == 200, (
                    f"Health endpoint should be accessible without auth, got {resp.status_code}"
                )
        finally:
            await spawner.destroy(name)
