"""Step executor dispatch — selects the right executor based on spawn mode.

No temporalio imports.
"""

from __future__ import annotations

import logging
from typing import Any

from cloud_agents.workflow.executor.step.base import StepExecutor

logger = logging.getLogger(__name__)


def get_step_executor(
    step: dict[str, Any],
    spawner: Any,
    transcript_store: Any = None,
) -> StepExecutor:
    """Create the right StepExecutor based on step.spawn mode.

    Parameters:
        step: Step definition dict.
        spawner: AgentSpawner instance (required for ephemeral).
        provider: LLM provider config.
        transcript_store: Optional TranscriptStore.

    Returns:
        StepExecutor for the step's spawn mode.

    Raises:
        ValueError: If spawn mode is unknown or ephemeral without spawner.
        NotImplementedError: If spawn mode is not yet implemented.
    """
    mode = step.get("spawn", "ephemeral")
    step_name = step.get("name", "unknown")

    if mode == "none":
        from cloud_agents.workflow.executor.step.direct import DirectExecutor

        return DirectExecutor()

    if mode == "local":
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        return SubprocessExecutor()

    if mode == "ephemeral":
        if spawner is None:
            raise ValueError(
                f"Step '{step_name}' requires spawn: ephemeral but no spawner "
                "is configured. Either deploy an OpenShell gateway and set "
                "OPENSHELL_GATEWAY_URL, or change the step to spawn: local or spawn: none."
            )

        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        return SandboxExecutor(spawner=spawner, transcript_store=transcript_store)

    raise ValueError(
        f"Unknown spawn mode '{mode}' for step '{step_name}'. "
        "Valid values: none, local, ephemeral."
    )
