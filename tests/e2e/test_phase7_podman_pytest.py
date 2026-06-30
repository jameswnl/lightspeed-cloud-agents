"""Pytest-compatible E2E tests for Phase 7 security on Podman.

Requires: podman running, agent-runtime:latest built, OPENAI_API_KEY set.
Run with: uv run pytest tests/e2e/test_phase7_podman_pytest.py -v --timeout=120
Skip with: uv run pytest -m "not e2e"
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def spawner():
    """Create a PodmanSpawner for the test session."""
    from cloud_agents.spawner.podman_spawner import PodmanSpawner

    os.system(
        "podman network exists cloud-agents 2>/dev/null || podman network create cloud-agents >/dev/null 2>&1"
    )
    return PodmanSpawner(
        network="cloud-agents",
        volume_mounts={
            os.path.abspath(
                os.path.join(
                    REPO_ROOT, "examples/agents/definitions/diagnostic-agent.yaml"
                )
            ): "/app/agent.yaml",
            os.path.abspath(
                os.path.join(REPO_ROOT, "examples/agents/tools/diagnostic_tools.py")
            ): "/app/tools/diagnostic_tools.py",
        },
    )


@pytest.fixture(scope="module")
def agent_env():
    """Environment variables for the agent container."""
    return {
        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4"),
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "https://api.openai.com/v1"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "AGENT_API_TOKEN": "pytest-e2e-token",
    }


@pytest.fixture(scope="module")
def agent_endpoint(spawner, agent_env):
    """Spawn an agent container and return its endpoint. Destroy after tests."""
    loop = asyncio.new_event_loop()
    endpoint = loop.run_until_complete(
        spawner.spawn("pytest-auth", "localhost/agent-runtime:latest", agent_env)
    )
    ready = loop.run_until_complete(spawner.wait_ready(endpoint, timeout=60.0))
    assert ready, f"Agent did not become ready at {endpoint}"
    yield endpoint
    loop.run_until_complete(spawner.destroy("pytest-auth"))
    loop.close()


class TestBearerAuth:
    """E2E tests for bearer auth on agent endpoints."""

    def test_unauthenticated_rejected(self, agent_endpoint) -> None:
        """Test that unauthenticated calls get 401."""
        import httpx

        resp = httpx.post(
            f"{agent_endpoint}/v1/run",
            json={"prompt": "test"},
            timeout=30.0,
        )
        assert resp.status_code == 401

    def test_authenticated_accepted(self, agent_endpoint) -> None:
        """Test that authenticated calls are accepted (not 401)."""
        import httpx

        resp = httpx.post(
            f"{agent_endpoint}/v1/run",
            json={"prompt": "list all hosts"},
            headers={"Authorization": "Bearer pytest-e2e-token"},
            timeout=120.0,
        )
        assert resp.status_code == 200

    def test_healthz_exempt(self, agent_endpoint) -> None:
        """Test that healthz is exempt from auth."""
        import httpx

        resp = httpx.get(f"{agent_endpoint}/healthz", timeout=10.0)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_wrong_token_rejected(self, agent_endpoint) -> None:
        """Test that wrong token gets 401."""
        import httpx

        resp = httpx.post(
            f"{agent_endpoint}/v1/run",
            json={"prompt": "test"},
            headers={"Authorization": "Bearer wrong-token"},
            timeout=30.0,
        )
        assert resp.status_code == 401
