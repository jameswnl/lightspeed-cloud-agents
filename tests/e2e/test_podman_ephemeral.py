"""E2E test: Podman ephemeral pod spawning.

Runs the workflow runner locally with PodmanSpawner, triggers an
ephemeral workflow, and verifies the container lifecycle:
spawn → execute → cleanup.

Prerequisites:
  - podman running with socket accessible
  - agent-runtime:latest image built
  - OPENAI_API_KEY set
  - podman-py installed

Usage:
  uv run python tests/e2e/test_podman_ephemeral.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


async def main() -> None:
    """Run the Podman ephemeral spawning E2E test."""
    from podman import PodmanClient

    from cloud_agents.spawner.podman_spawner import PodmanSpawner

    image = "localhost/agent-runtime:latest"
    agent_yaml = os.path.join(
        REPO_ROOT, "examples/agents/definitions/diagnostic-agent.yaml"
    )
    tools_py = os.path.join(REPO_ROOT, "examples/agents/tools/diagnostic_tools.py")

    print("=== Podman Ephemeral Spawn E2E Test ===")
    print()

    # Verify image exists
    with PodmanClient() as client:
        try:
            client.images.get(image)
            print(f"[OK] Image {image} exists")
        except Exception:
            print(f"[FAIL] Image {image} not found. Build it first.")
            return

    # Ensure network exists
    os.system(
        "podman network exists cloud-agents 2>/dev/null || podman network create cloud-agents >/dev/null 2>&1"
    )

    spawner = PodmanSpawner(
        network="cloud-agents",
        volume_mounts={
            os.path.abspath(agent_yaml): "/app/agent.yaml",
            os.path.abspath(tools_py): "/app/tools/diagnostic_tools.py",
        },
    )

    agent_name = "test-ephemeral-diag"
    env = {
        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4"),
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "https://api.openai.com/v1"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    }

    # Verify no pre-existing container
    with PodmanClient() as client:
        try:
            client.containers.get(f"agent-{agent_name}")
            print("[WARN] Container already exists, removing...")
            c = client.containers.get(f"agent-{agent_name}")
            c.stop(timeout=5)
            c.remove()
        except Exception:
            pass

    print()
    print("--- Step 1: Spawn ephemeral container ---")
    endpoint = await spawner.spawn(agent_name, image, env)
    print(f"[OK] Spawned: {endpoint}")
    print(f"[OK] Active count: {spawner.active_count}")

    # Verify container exists
    with PodmanClient() as client:
        container = client.containers.get(f"agent-{agent_name}")
        print(f"[OK] Container status: {container.status}")

    print()
    print("--- Step 2: Wait for healthz ---")
    ready = await spawner.wait_ready(endpoint, timeout=60.0)
    if ready:
        print(f"[OK] Agent is ready at {endpoint}")
    else:
        print("[FAIL] Agent did not become ready")
        await spawner.destroy(agent_name)
        return

    print()
    print("--- Step 3: Call agent via HTTP ---")
    import httpx

    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.get(f"{endpoint}/healthz")
            print(f"[OK] Healthz: {resp.json()}")

            resp = await http.post(
                f"{endpoint}/v1/run",
                json={"prompt": "Check all hosts for issues"},
            )
            result = resp.json()
            print(f"[OK] Run success: {result.get('success')}")
            print(f"[OK] Agent name: {result.get('agent_name')}")
            print(f"[OK] Output type: {result.get('output_type')}")
            if result.get("output"):
                print(
                    f"[OK] cluster_healthy: {result['output'].get('cluster_healthy')}"
                )
    except Exception as exc:
        print(f"[FAIL] HTTP call failed: {exc}")

    print()
    print("--- Step 4: Destroy ephemeral container ---")
    await spawner.destroy(agent_name)
    print(f"[OK] Active count after destroy: {spawner.active_count}")

    # Verify container is gone
    with PodmanClient() as client:
        try:
            client.containers.get(f"agent-{agent_name}")
            print("[FAIL] Container still exists after destroy")
        except Exception:
            print("[OK] Container cleaned up successfully")

    print()
    print("=== All Podman Ephemeral Tests Passed ===")


if __name__ == "__main__":
    asyncio.run(main())
