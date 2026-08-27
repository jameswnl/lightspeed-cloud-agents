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
    middlewares: Any = None,
) -> WorkflowRunner:
    """Create a WorkflowRunner based on WORKFLOW_ENGINE env var.

    Parameters:
        spawner: AgentSpawner instance for sandbox lifecycle.
        run_state_store: PostgreSQL RunStateStore (required for local).
        transcript_store: Optional TranscriptStore.
        middlewares: Optional list of StepMiddleware instances for chat runner.

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
    elif engine == "chat":
        return _create_chat(
            spawner=spawner,
            run_state_store=run_state_store,
            transcript_store=transcript_store,
            middlewares=middlewares,
        )
    else:
        raise ValueError(
            f"Unknown WORKFLOW_ENGINE='{engine}'. " "Valid values: 'local', 'temporal', 'chat'"
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


def _create_chat(
    *,
    spawner: Any = None,
    run_state_store: Any = None,
    transcript_store: Any = None,
    middlewares: Any = None,
) -> WorkflowRunner:
    """Create a ChatWorkflowRunner with configuration from env vars.

    Parameters:
        spawner: AgentSpawner instance for sandbox lifecycle.
        run_state_store: PostgreSQL RunStateStore.
        transcript_store: Optional TranscriptStore.
        middlewares: Optional list of StepMiddleware instances.

    Returns:
        ChatWorkflowRunner instance.

    Raises:
        ValueError: If required stores are not provided.
    """
    if run_state_store is None:
        raise ValueError(
            "WORKFLOW_ENGINE=chat requires a RunStateStore. "
            "Set RUN_STATE_DB_URL to a PostgreSQL connection URL."
        )
    if transcript_store is None:
        raise ValueError(
            "WORKFLOW_ENGINE=chat requires a TranscriptStore. "
            "Set TRANSCRIPT_DB_URL to a PostgreSQL connection URL."
        )

    from cloud_agents.workflow.executor.chat.runner import (
        ChatWorkflowConfig,
        ChatWorkflowRunner,
    )

    # Build config from env vars
    provider_name = os.environ.get("CHAT_PROVIDER_NAME", "openai")
    provider_model = os.environ.get("CHAT_PROVIDER_MODEL", "gpt-4o")
    provider_secret = os.environ.get("CHAT_PROVIDER_SECRET", "openai-api-key")
    system_prompt = os.environ.get("CHAT_SYSTEM_PROMPT")
    max_context_turns = int(os.environ.get("CHAT_MAX_CONTEXT_TURNS", "20"))
    spawn_mode = os.environ.get("CHAT_SPAWN_MODE", "none")
    chat_skills_raw = os.environ.get("CHAT_ALLOWED_SKILLS", "")
    chat_allowed_skills = (
        [s.strip() for s in chat_skills_raw.split(",") if s.strip()] if chat_skills_raw else None
    )

    config = ChatWorkflowConfig(
        provider={
            "name": provider_name,
            "model": provider_model,
            "credentials_secret": provider_secret,
        },
        system_prompt=system_prompt,
        max_context_turns=max_context_turns,
        spawn=spawn_mode,
        allowed_skills=chat_allowed_skills,
    )

    logger.info("Using ChatWorkflowRunner (WORKFLOW_ENGINE=chat)")
    return ChatWorkflowRunner(
        run_store=run_state_store,
        transcript_store=transcript_store,
        config=config,
        spawner=spawner,
        middlewares=middlewares,
    )
