"""Local workflow runner entrypoint.

Builds a FastAPI app with the LocalWorkflowRunner — no Temporal dependency.
Uses pydantic-graph for workflow orchestration and PostgreSQL for
run-state persistence.

Usage: uvicorn cloud_agents.workflow.executor.local.entrypoint:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cloud_agents.runtime.logging import configure_logging
from cloud_agents.runtime.tracing import init_tracing
from cloud_agents.storage.run_state_store import RunStateStore
from cloud_agents.storage.transcript_store import TranscriptStore
from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner
from cloud_agents.workflow.executor.temporal.entrypoint import (
    AUTH_REQUIRED,
    CONTENT_POLICY_PATH,
    _create_spawner,
    _get_auth_dependency,
    _load_content_policy,
    reconcile_orphaned_sandboxes,
)

logger = logging.getLogger(__name__)


def build_local_app() -> FastAPI:
    """Build FastAPI app with LocalWorkflowRunner — no Temporal required.

    Returns:
        FastAPI application with lifespan-managed stores and spawner.
    """
    configure_logging()
    init_tracing("workflow-runner")

    spawner = _create_spawner()
    transcript_store = TranscriptStore.from_env()
    run_state_store = RunStateStore.from_env()

    if transcript_store:
        logger.info("Transcript store configured (TRANSCRIPT_DB_URL set)")
    if run_state_store:
        logger.info("Run-state store configured (RUN_STATE_DB_URL set)")
    else:
        logger.warning(
            "RUN_STATE_DB_URL not set — LocalWorkflowRunner will run without persistence. "
            "Approval gates and status queries will not survive process restarts."
        )

    executor = LocalWorkflowRunner(
        spawner=spawner,
        run_state_store=run_state_store,
        transcript_store=transcript_store,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Connect stores and clean up orphans on startup."""
        await reconcile_orphaned_sandboxes(spawner)

        if transcript_store:
            try:
                await transcript_store.connect()
            except Exception as exc:
                logger.warning("Failed to connect transcript store: %s", exc)

        if run_state_store:
            await run_state_store.connect()
            paused = await executor.recover_paused()
            if paused:
                logger.info("Found %d paused workflows on startup", len(paused))

        yield

        if transcript_store:
            try:
                await transcript_store.close()
            except Exception:
                logger.debug("Error closing transcript store", exc_info=True)
        if run_state_store:
            try:
                await run_state_store.close()
            except Exception:
                logger.debug("Error closing run-state store", exc_info=True)

    app = FastAPI(title="Cloud Agents Workflow Runner (Local)", lifespan=lifespan)

    cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cors_origins.split(",")],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Add rate-limit and content-size middleware
    from cloud_agents.runtime.middleware import ContentSizeLimitMiddleware

    app.add_middleware(ContentSizeLimitMiddleware)

    if os.environ.get("RATE_LIMIT_ENABLED", "").lower() == "true":
        from cloud_agents.runtime.rate_limiter import RateLimitMiddleware

        app.add_middleware(RateLimitMiddleware)

    # Register built-in tools before building the API router
    from cloud_agents.tools import load_builtin_tools

    load_builtin_tools()

    # Load custom tools module if configured
    tools_module = os.environ.get("CLOUD_AGENTS_TOOLS_MODULE")
    if tools_module:
        from cloud_agents.workflow.executor.step.tools import load_tools_module

        load_tools_module(tools_module)

    # Build the API router using the local executor
    from cloud_agents.workflow.executor.local.api import build_local_router

    auth_dep = _get_auth_dependency()
    content_policy = _load_content_policy() if CONTENT_POLICY_PATH else None

    router = build_local_router(
        executor=executor,
        get_caller_identity=auth_dep,
        content_policy=content_policy,
    )
    app.include_router(router, prefix="/v1/workflows")

    # Mount chat router if stores are available
    if run_state_store and transcript_store:
        from cloud_agents.workflow.executor.chat.api import build_chat_router
        from cloud_agents.workflow.executor.chat.runner import (
            ChatWorkflowConfig,
            ChatWorkflowRunner,
        )

        _chat_skills_raw = os.environ.get("CHAT_ALLOWED_SKILLS", "")
        _chat_allowed = (
            [s.strip() for s in _chat_skills_raw.split(",") if s.strip()] if _chat_skills_raw else None
        )
        chat_config = ChatWorkflowConfig(
            provider={
                "name": os.environ.get("CHAT_PROVIDER", "openai"),
                "model": os.environ.get("CHAT_MODEL", "gpt-4o"),
                "credentials_secret": os.environ.get("CHAT_CREDENTIALS_SECRET", "openai-api-key"),
            },
            system_prompt=os.environ.get("CHAT_SYSTEM_PROMPT"),
            allowed_skills=_chat_allowed,
        )
        chat_runner = ChatWorkflowRunner(
            run_store=run_state_store,
            transcript_store=transcript_store,
            config=chat_config,
            spawner=spawner,
        )
        chat_router = build_chat_router(chat_runner)
        app.include_router(chat_router, prefix="/v1")
        logger.info("Chat router mounted at /v1/chat")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    logger.info("Local workflow runner ready (WORKFLOW_ENGINE=local)")
    return app


app = build_local_app()
