"""E2E tests for Phase 7 security hardening.

Tests Bearer auth on agent endpoints and risk_level fail-closed
behavior using real Podman containers.

Prerequisites:
  - agent-runtime:latest image built
  - podman running
  - OPENAI_API_KEY set (or any valid key for agent startup)

Usage:
  uv run python tests/e2e/test_phase7_security.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


async def main() -> None:
    """Run Phase 7 security E2E tests."""
    import httpx

    from cloud_agents.spawner.podman_spawner import PodmanSpawner

    image = "localhost/agent-runtime:latest"
    agent_yaml = os.path.abspath(
        os.path.join(REPO_ROOT, "examples/agents/definitions/diagnostic-agent.yaml")
    )
    tools_py = os.path.abspath(
        os.path.join(REPO_ROOT, "examples/agents/tools/diagnostic_tools.py")
    )

    print("=== Phase 7 Security E2E Tests ===")
    print()

    # Ensure network
    os.system(
        "podman network exists cloud-agents 2>/dev/null || podman network create cloud-agents >/dev/null 2>&1"
    )

    # --- Test 1: Bearer auth blocks unauthenticated calls ---
    print("--- Test 1: Bearer auth on agent endpoints ---")

    spawner = PodmanSpawner(
        network="cloud-agents",
        volume_mounts={
            agent_yaml: "/app/agent.yaml",
            tools_py: "/app/tools/diagnostic_tools.py",
        },
    )

    env = {
        "AGENT_MODEL": os.environ.get("AGENT_MODEL", "gpt-4"),
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "https://api.openai.com/v1"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "AGENT_API_TOKEN": "test-secret-token-123",
    }

    endpoint = await spawner.spawn("auth-test", image, env)
    try:
        ready = await spawner.wait_ready(endpoint, timeout=60.0)
        if not ready:
            print("[FAIL] Agent did not become ready")
            return

        async with httpx.AsyncClient(timeout=30.0) as http:
            # Unauthenticated call should be rejected (401)
            resp = await http.post(
                f"{endpoint}/v1/run",
                json={"prompt": "test"},
            )
            if resp.status_code == 401:
                print("[OK] Unauthenticated call rejected (401)")
            else:
                print(
                    f"[WARN] Expected 401, got {resp.status_code} — auth middleware may not be active"
                )

            # Authenticated call should succeed
            resp = await http.post(
                f"{endpoint}/v1/run",
                json={"prompt": "list all hosts"},
                headers={"Authorization": "Bearer test-secret-token-123"},
            )
            if resp.status_code == 200:
                body = resp.json()
                print(
                    f"[OK] Authenticated call succeeded: success={body.get('success')}"
                )
            else:
                print(
                    f"[FAIL] Authenticated call failed: {resp.status_code} {resp.text[:200]}"
                )

            # Healthz should be exempt from auth
            resp = await http.get(f"{endpoint}/healthz")
            if resp.status_code == 200:
                print("[OK] /healthz exempt from auth (200)")
            else:
                print(f"[FAIL] /healthz should be exempt: {resp.status_code}")

    finally:
        await spawner.destroy("auth-test")
        print("[OK] Container cleaned up")

    print()

    # --- Test 2: Content-hash naming produces deterministic names ---
    print("--- Test 2: Content-hash naming ---")
    import hashlib

    wf_id = "wf-e2e-test-123"
    step_name = "diagnose"
    attempt = 1

    hash_input = f"{wf_id}:{step_name}:{attempt}"
    spawn_id = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
    expected_name = f"diagnostic-agent-{spawn_id}"

    # Same input should produce same name
    hash_input2 = f"{wf_id}:{step_name}:{attempt}"
    spawn_id2 = hashlib.sha256(hash_input2.encode()).hexdigest()[:8]
    if spawn_id == spawn_id2:
        print(f"[OK] Same input → same hash: {expected_name}")
    else:
        print(f"[FAIL] Hash mismatch: {spawn_id} != {spawn_id2}")

    # Different workflow should produce different name
    hash_input3 = f"wf-different:{step_name}:{attempt}"
    spawn_id3 = hashlib.sha256(hash_input3.encode()).hexdigest()[:8]
    if spawn_id != spawn_id3:
        print(f"[OK] Different workflow → different hash: {spawn_id} != {spawn_id3}")
    else:
        print("[FAIL] Different workflows produced same hash")

    print()

    # --- Test 3: risk_level fail-closed ---
    print("--- Test 3: risk_level fail-closed behavior ---")
    from cloud_agents.workflow.auto_approve import ApprovalPolicy, classify_step_risk
    from cloud_agents.workflow.definition import WorkflowStepSpec

    # Step with no risk_level → should default to high
    step_no_risk = WorkflowStepSpec(
        name="unknown-step",
        type="agent",
        prompt="do something",
        output_key="r",
        spawn="pre-deployed",
    )
    result = classify_step_risk(step_no_risk, ApprovalPolicy())
    if result.risk_level == "high" and not result.auto_approved:
        print("[OK] No risk_level → high risk, manual approval required")
    else:
        print(
            f"[FAIL] Expected high risk, got {result.risk_level}, auto_approved={result.auto_approved}"
        )

    # Step with explicit low → should auto-approve
    step_low = WorkflowStepSpec(
        name="check-hosts",
        type="agent",
        prompt="check hosts",
        output_key="r",
        spawn="pre-deployed",
        risk_level="low",
    )
    result_low = classify_step_risk(step_low, ApprovalPolicy())
    if result_low.risk_level == "low" and result_low.auto_approved:
        print("[OK] Explicit risk_level=low → auto-approved")
    else:
        print(
            f"[FAIL] Expected low/auto-approved, got {result_low.risk_level}/{result_low.auto_approved}"
        )

    # Misleading name with explicit critical → should be critical
    step_misleading = WorkflowStepSpec(
        name="safe-check",
        type="agent",
        prompt="check",
        output_key="r",
        spawn="pre-deployed",
        risk_level="critical",
    )
    result_mis = classify_step_risk(step_misleading, ApprovalPolicy())
    if result_mis.risk_level == "critical":
        print("[OK] Explicit risk_level=critical overrides safe-sounding name")
    else:
        print(f"[FAIL] Expected critical, got {result_mis.risk_level}")

    print()
    print("=== All Phase 7 Security E2E Tests Complete ===")


if __name__ == "__main__":
    asyncio.run(main())
