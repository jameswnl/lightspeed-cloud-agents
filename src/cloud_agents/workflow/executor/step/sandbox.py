"""SandboxExecutor — runs steps in OpenShell containers.

Wraps step_runner.run_step() behind the StepExecutor interface.
This is the spawn: ephemeral implementation.
"""

from __future__ import annotations

import logging
from typing import Any

from cloud_agents.workflow.executor.step.base import StepExecutor, StepInput, StepResult

logger = logging.getLogger(__name__)


class SandboxExecutor(StepExecutor):
    """Execute steps in ephemeral sandbox containers via OpenShell.

    Wraps the existing step_runner.run_step() logic behind the
    StepExecutor interface for backward compatibility.
    """

    def __init__(self, spawner: Any = None, transcript_store: Any = None) -> None:
        """Initialize with a spawner and optional transcript store.

        Parameters:
            spawner: AgentSpawner instance for sandbox lifecycle.
            transcript_store: Optional TranscriptStore for persistence.
        """
        self._spawner = spawner
        self._transcript_store = transcript_store

    async def run(self, step_input: StepInput) -> StepResult:
        """Execute step in a sandbox container.

        Parameters:
            step_input: Step execution input.

        Returns:
            StepResult with status, output, transcript, and metrics.

        Raises:
            ValueError: If no spawner is configured.
        """
        if self._spawner is None:
            raise ValueError(
                f"Step '{step_input.step_name}' requires spawn: ephemeral but no "
                "spawner is configured. Either deploy an OpenShell gateway and set "
                "OPENSHELL_GATEWAY_URL, or change the step to spawn: local or spawn: none."
            )

        from cloud_agents.workflow.core.step_runner import run_step

        step_dict = step_input.raw_step or {
            "name": step_input.step_name,
            "prompt": step_input.prompt,
            "output_key": step_input.output_key,
        }

        run_step_input = {
            "step": step_dict,
            "workflow_id": step_input.workflow_id,
            "provider": step_input.provider,
            "sandbox_image": step_input.sandbox_image,
            "skills_image": step_input.skills_image,
            "skills_paths": step_input.skills_paths,
            "mcp_servers": step_input.mcp_servers,
            "context": step_input.context,
        }

        result = await run_step(
            run_step_input,
            spawner=self._spawner,
            transcript_store=self._transcript_store,
            attempt=1,
        )

        transcript_data = result.get("transcript", {})
        transcript_events = []
        if isinstance(transcript_data, dict):
            transcript_events = transcript_data.get("events", [])

        return StepResult(
            status=result.get("status", "failed"),
            output=result.get("output"),
            error=result.get("error"),
            transcript=transcript_events,
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        )
