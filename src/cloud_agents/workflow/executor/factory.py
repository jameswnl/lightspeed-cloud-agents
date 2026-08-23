"""Runner factory — creates the right WorkflowRunner based on config.

Reads WORKFLOW_ENGINE env var to select between local (pydantic-graph)
and temporal backends. Fail-fast validation for incompatible configs.

No temporalio imports — Temporal is imported lazily only when selected.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cloud_agents.workflow.executor.base import WorkflowRunner

logger = logging.getLogger(__name__)


def create_runner(
    *,
    spawner: Any = None,
    run_state_store: Any = None,
    transcript_store: Any = None,
) -> WorkflowRunner:
    """Create a WorkflowRunner based on WORKFLOW_ENGINE env var.

    Parameters:
        spawner: AgentSpawner instance for sandbox lifecycle.
        run_state_store: PostgreSQL RunStateStore (required for local).
        transcript_store: Optional TranscriptStore.

    Returns:
        WorkflowRunner instance (LocalWorkflowRunner or TemporalWorkflowRunner).

    Raises:
        ValueError: If config is invalid or incompatible.
    """
    engine = os.environ.get("WORKFLOW_ENGINE", "local")

    if engine == "local":
        return _create_local(
            spawner=spawner,
            run_state_store=run_state_store,
            transcript_store=transcript_store,
        )
    elif engine == "temporal":
        return _create_temporal()
    else:
        raise ValueError(
            f"Unknown WORKFLOW_ENGINE='{engine}'. Valid values: 'local', 'temporal'"
        )


def _create_local(
    *,
    spawner: Any = None,
    run_state_store: Any = None,
    transcript_store: Any = None,
) -> WorkflowRunner:
    """Create a LocalWorkflowRunner with fail-fast validation."""
    # Fail fast: alert/schedule triggers require Temporal
    if os.environ.get("ALERT_TRIGGER_ENABLED", "").lower() == "true":
        raise ValueError(
            "ALERT_TRIGGER_ENABLED=true requires WORKFLOW_ENGINE=temporal. "
            "Alert triggers are not supported with the local executor."
        )
    if os.environ.get("SCHEDULE_ENABLED", "").lower() == "true":
        raise ValueError(
            "SCHEDULE_ENABLED=true requires WORKFLOW_ENGINE=temporal. "
            "Schedule triggers are not supported with the local executor."
        )

    from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

    logger.info("Using LocalWorkflowRunner (WORKFLOW_ENGINE=local)")
    return LocalWorkflowRunner(
        spawner=spawner,
        run_state_store=run_state_store,
        transcript_store=transcript_store,
    )


def _create_temporal() -> WorkflowRunner:
    """Create a TemporalWorkflowRunner with fail-fast validation.

    Note: The actual Temporal Client connection happens in the entrypoint
    lifespan, not here. This factory validates config and returns a
    placeholder that will be connected later.
    """
    temporal_url = os.environ.get("TEMPORAL_URL", "")
    if not temporal_url:
        raise ValueError(
            "WORKFLOW_ENGINE=temporal requires TEMPORAL_URL to be set. "
            "Set TEMPORAL_URL to the Temporal Server gRPC address "
            "(e.g. 'localhost:7233')."
        )

    # TemporalWorkflowRunner needs a connected Client, which is created
    # during the FastAPI lifespan. Return a lazy wrapper that raises
    # if used before connection.
    logger.info(
        "Using TemporalWorkflowRunner (WORKFLOW_ENGINE=temporal, url=%s)",
        temporal_url,
    )

    from cloud_agents.workflow.executor.temporal.executor import TemporalWorkflowRunner

    # Return with None client — the entrypoint will set it after connecting
    return TemporalWorkflowRunner(client=None)  # type: ignore[arg-type]
