"""E2E test: full-stack workflow with real containers and real LLM calls.

Exercises the complete spawn -> HTTP -> LLM -> destroy lifecycle
using a real spawner (PodmanSpawner or KubernetesSpawner) and a
real LLM provider. This proves the orchestration works beyond
stub-mode activities.

Prerequisites:
  - Podman running with socket accessible (or Kind cluster)
  - lightspeed-agentic-sandbox:temporal image built
  - OPENAI_API_KEY (or equivalent) set in environment
  - Temporal server running (TEMPORAL_E2E_URL)

Usage:
  OPENAI_API_KEY=sk-... TEMPORAL_E2E_URL=localhost:7233 \
    uv run pytest tests/e2e/test_full_stack.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

TEMPORAL_URL = os.environ.get("TEMPORAL_E2E_URL", "localhost:7233")
SANDBOX_IMAGE = os.environ.get(
    "SANDBOX_IMAGE", "localhost/lightspeed-agentic-sandbox:temporal"
)
WORKFLOW_YAML = (
    Path(__file__).parents[2]
    / "examples"
    / "workflow-definitions"
    / "ephemeral-diagnose-workflow.yaml"
)


def _has_llm_key() -> bool:
    """Check if an LLM API key is available."""
    return bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )


def _get_provider_config() -> dict:
    """Build provider config from available environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return {
            "name": "openai",
            "model": "gpt-4o-mini",
            "credentials_secret": "OPENAI_API_KEY",
        }
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "name": "claude",
            "model": "claude-sonnet-4-20250514",
            "credentials_secret": "ANTHROPIC_API_KEY",
        }
    if os.environ.get("GOOGLE_API_KEY"):
        return {
            "name": "gemini",
            "model": "gemini-2.0-flash",
            "credentials_secret": "GOOGLE_API_KEY",
        }
    return {
        "name": "openai",
        "model": "gpt-4o-mini",
        "credentials_secret": "OPENAI_API_KEY",
    }


@pytest.mark.skipif(
    not _has_llm_key(),
    reason="Full-stack tests require an LLM API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY)",
)
@pytest.mark.asyncio
class TestFullStackWorkflow:
    """Run a real workflow with real sandbox containers and real LLM calls."""

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

    async def test_single_step_real_llm(self, spawner) -> None:
        """Single-step workflow completes with real LLM output.

        Spawns a real sandbox container, sends a prompt to a real LLM,
        and verifies the output is non-stub (not 'executed-...' placeholder).
        """
        from temporalio.client import Client
        from temporalio.worker import Worker

        from cloud_agents.workflow.temporal_activities import (
            build_escalation_activity,
            run_sandbox_step,
            send_approval_notification,
        )
        from cloud_agents.workflow.temporal_models import ProviderConfig, WorkflowInput
        from cloud_agents.workflow.temporal_worker import build_worker_config
        from cloud_agents.workflow.temporal_workflow import AgentWorkflow

        client = await Client.connect(TEMPORAL_URL)
        queue = f"e2e-full-{uuid.uuid4().hex[:8]}"
        wf_id = f"e2e-full-stack-{uuid.uuid4().hex[:8]}"

        # Use a simple one-step workflow
        definition = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "full-stack-test"},
            "spec": {
                "steps": [
                    {
                        "name": "analyze",
                        "type": "agent",
                        "prompt": "Say 'hello world' and nothing else.",
                        "output_key": "result",
                        "timeout_seconds": 120,
                    },
                ],
            },
        }

        provider = ProviderConfig(**_get_provider_config())
        wf_input = WorkflowInput(
            definition=definition,
            workflow_id=wf_id,
            provider=provider,
            sandbox_image=SANDBOX_IMAGE,
        )

        # Build worker with real spawner so activities actually spawn containers
        worker_config = build_worker_config(spawner=spawner)

        async with Worker(
            client,
            task_queue=queue,
            workflows=worker_config.workflows,
            activities=worker_config.activities,
        ):
            result = await client.execute_workflow(
                AgentWorkflow.run,
                wf_input,
                id=wf_id,
                task_queue=queue,
                execution_timeout=timedelta(seconds=300),
            )

        # Verify real LLM output (not stub placeholder)
        assert "result" in result.steps, "result step missing from output"
        step_result = result.steps["result"]
        assert step_result.status == "completed", (
            f"Expected completed, got {step_result.status}: {step_result.error}"
        )
        assert step_result.output is not None, "Expected non-None output from real LLM"
        # Stub mode returns {"summary": "executed-analyze"} — real output is different
        output_str = str(step_result.output)
        assert "executed-analyze" not in output_str, (
            "Output looks like a stub result, not real LLM output"
        )

    async def test_no_sandbox_containers_left_running(self, spawner) -> None:
        """After workflow completes, no sandbox containers remain.

        Validates the cleanup path: spawner.destroy() is called after
        the activity finishes, leaving no orphaned containers.
        """
        from temporalio.client import Client
        from temporalio.worker import Worker

        from cloud_agents.workflow.temporal_models import ProviderConfig, WorkflowInput
        from cloud_agents.workflow.temporal_worker import build_worker_config
        from cloud_agents.workflow.temporal_workflow import AgentWorkflow

        client = await Client.connect(TEMPORAL_URL)
        queue = f"e2e-cleanup-{uuid.uuid4().hex[:8]}"
        wf_id = f"e2e-cleanup-{uuid.uuid4().hex[:8]}"

        definition = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "cleanup-test"},
            "spec": {
                "steps": [
                    {
                        "name": "quick",
                        "type": "agent",
                        "prompt": "Respond with OK.",
                        "output_key": "r1",
                        "timeout_seconds": 120,
                    },
                ],
            },
        }

        provider = ProviderConfig(**_get_provider_config())
        wf_input = WorkflowInput(
            definition=definition,
            workflow_id=wf_id,
            provider=provider,
            sandbox_image=SANDBOX_IMAGE,
        )

        worker_config = build_worker_config(spawner=spawner)

        async with Worker(
            client,
            task_queue=queue,
            workflows=worker_config.workflows,
            activities=worker_config.activities,
        ):
            await client.execute_workflow(
                AgentWorkflow.run,
                wf_input,
                id=wf_id,
                task_queue=queue,
                execution_timeout=timedelta(seconds=300),
            )

        # Check no containers with our workflow labels remain
        active = await spawner.list_active({"cloud-agents/workflow-id": wf_id})
        assert len(active) == 0, (
            f"Expected no containers after workflow completion, found: {active}"
        )
