"""Temporal workflow runner entrypoint.

Builds a FastAPI app with Temporal workflow endpoints. The Temporal
client and worker are created in the app lifespan and shut down on exit.

Usage: uvicorn agents.workflow.temporal_entrypoint:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.worker import Worker

from agents.workflow.temporal_api import build_temporal_router
from agents.workflow.temporal_worker import build_worker_config

logger = logging.getLogger(__name__)

TEMPORAL_URL = os.environ.get("TEMPORAL_URL", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
WORKFLOW_ENGINE = os.environ.get("WORKFLOW_ENGINE", "temporal")


def build_temporal_app(
    temporal_url: str = TEMPORAL_URL,
    temporal_namespace: str = TEMPORAL_NAMESPACE,
) -> FastAPI:
    """Build FastAPI app with Temporal workflow endpoints.

    Parameters:
        temporal_url: Temporal Server gRPC address.
        temporal_namespace: Temporal namespace.

    Returns:
        FastAPI application with lifespan-managed Temporal client and worker.
    """
    worker_config = build_worker_config()
    temporal_client_holder: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Connect Temporal client and start worker on startup."""
        client = await Client.connect(temporal_url, namespace=temporal_namespace)
        temporal_client_holder["client"] = client
        logger.info(
            "Connected to Temporal at %s (namespace=%s)",
            temporal_url, temporal_namespace,
        )

        async with Worker(
            client,
            task_queue=worker_config.task_queue,
            workflows=worker_config.workflows,
            activities=worker_config.activities,
            max_concurrent_activities=worker_config.max_concurrent_activities,
        ):
            logger.info("Temporal worker started on queue '%s'", worker_config.task_queue)
            yield

        logger.info("Temporal worker stopped")

    app = FastAPI(title="Cloud Agents Workflow Runner (Temporal)", lifespan=lifespan)

    placeholder_client = _DeferredClient(temporal_client_holder)
    router = build_temporal_router(placeholder_client)  # type: ignore[arg-type]
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    return app


class _DeferredClient:
    """Proxy that delegates to a Temporal Client set during lifespan."""

    def __init__(self, holder: dict[str, Client]) -> None:
        self._holder = holder

    def __getattr__(self, name: str):
        """Delegate attribute access to the held client."""
        return getattr(self._holder["client"], name)
