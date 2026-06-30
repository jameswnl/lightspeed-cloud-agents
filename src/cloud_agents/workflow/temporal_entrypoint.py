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

from agents.runtime.tracing import init_tracing
from agents.workflow.definition_store import DefinitionStore
from agents.workflow.structured_logging import configure_logging
from agents.workflow.temporal_api import build_temporal_router
from agents.workflow.temporal_worker import build_worker_config

logger = logging.getLogger(__name__)


def _get_tracing_interceptors() -> list:
    """Get Temporal tracing interceptors if OTel is available."""
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor

        return [TracingInterceptor()]
    except Exception:
        return []


TEMPORAL_URL = os.environ.get("TEMPORAL_URL", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
WORKFLOW_ENGINE = os.environ.get("WORKFLOW_ENGINE", "temporal")


def _build_tls_config():
    """Build TLS config from environment variables.

    Returns None if TLS is not enabled.
    """
    if os.environ.get("TEMPORAL_TLS_ENABLED", "").lower() != "true":
        return None

    from temporalio.service import TLSConfig

    cert_path = os.environ.get("TEMPORAL_TLS_CERT_PATH")
    key_path = os.environ.get("TEMPORAL_TLS_KEY_PATH")
    ca_path = os.environ.get("TEMPORAL_TLS_CA_PATH")

    client_cert = open(cert_path, "rb").read() if cert_path else None
    client_key = open(key_path, "rb").read() if key_path else None
    server_root_ca = open(ca_path, "rb").read() if ca_path else None

    return TLSConfig(
        client_cert=client_cert,
        client_private_key=client_key,
        server_root_ca_cert=server_root_ca,
    )


SPAWNER_TYPE = os.environ.get("WORKFLOW_SPAWNER", "")


def _create_spawner():
    """Create spawner based on environment config."""
    if SPAWNER_TYPE == "kubernetes":
        from agents.spawner.kubernetes_spawner import KubernetesSpawner

        namespace = os.environ.get("SPAWNER_NAMESPACE", "default")
        service_account = os.environ.get("SPAWNER_SERVICE_ACCOUNT", "workflow-runner")
        logger.info("Using KubernetesSpawner (namespace=%s)", namespace)
        return KubernetesSpawner(namespace=namespace, service_account=service_account)
    if SPAWNER_TYPE == "podman":
        from agents.spawner.podman_spawner import PodmanSpawner

        network = os.environ.get("SPAWNER_NETWORK", "cloud-agents")
        logger.info("Using PodmanSpawner (network=%s)", network)
        return PodmanSpawner(network=network)
    logger.info("No spawner configured — sandbox activity will use stub mode")
    return None


async def reconcile_orphaned_sandboxes(spawner: "AgentSpawner | None") -> None:
    """Destroy orphaned sandbox containers left from a previous crash.

    On startup, scans for containers/Jobs with the "spawned-by=workflow-runner"
    label and destroys them. This prevents resource leaks after unclean shutdowns.

    Args:
        spawner: The spawner instance, or None if no spawner is configured.
    """
    if spawner is None:
        return

    orphans = await spawner.list_active({"spawned-by": "workflow-runner"})
    for name in orphans:
        logger.warning("Destroying orphaned sandbox '%s'", name)
        try:
            await spawner.destroy(name)
        except Exception as exc:
            logger.error("Failed to destroy orphaned sandbox '%s': %s", name, exc)
    if orphans:
        logger.info("Cleaned up %d orphaned sandbox(es) on startup", len(orphans))
        from agents.workflow.audit import emit_audit

        emit_audit(
            event_type="orphan_cleanup",
            workflow_id="startup",
            details={"count": len(orphans), "names": orphans},
        )


AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"


def _get_auth_dependency():
    """Load auth dependency from the stack configuration.

    Returns None in dev/test mode. Fails closed when AUTH_REQUIRED=true.
    """
    try:
        from authentication import get_auth_dependency

        return get_auth_dependency()
    except Exception:
        if AUTH_REQUIRED:
            raise RuntimeError(
                "AUTH_REQUIRED=true but auth dependency failed to initialize. "
                "Refusing to start with unauthenticated workflow endpoints."
            )
        logger.warning("Auth dependency not available — endpoints unauthenticated")
        return None


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
    configure_logging()
    init_tracing("workflow-runner")

    spawner = _create_spawner()
    worker_config = build_worker_config(spawner=spawner)
    temporal_client_holder: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Connect Temporal client and start worker on startup."""
        await reconcile_orphaned_sandboxes(spawner)
        try:
            tls_config = _build_tls_config()
            connect_kwargs: dict = {
                "target_host": temporal_url,
                "namespace": temporal_namespace,
            }
            if tls_config:
                connect_kwargs["tls"] = tls_config
                logger.info("Temporal TLS enabled")
            client = await Client.connect(**connect_kwargs)
            temporal_client_holder["client"] = client
            logger.info(
                "Connected to Temporal at %s (namespace=%s)",
                temporal_url,
                temporal_namespace,
            )

            async with Worker(
                client,
                task_queue=worker_config.task_queue,
                workflows=worker_config.workflows,
                activities=worker_config.activities,
                max_concurrent_activities=worker_config.max_concurrent_activities,
                interceptors=_get_tracing_interceptors(),
            ):
                logger.info(
                    "Temporal worker started on queue '%s'", worker_config.task_queue
                )
                yield

            logger.info("Temporal worker stopped")
        except Exception as exc:
            logger.warning(
                "Cannot connect to Temporal at %s: %s. "
                "App will serve healthz but workflows are unavailable.",
                temporal_url,
                exc,
            )
            yield

    app = FastAPI(title="Cloud Agents Workflow Runner (Temporal)", lifespan=lifespan)

    placeholder_client = _DeferredClient(temporal_client_holder)
    definition_store = DefinitionStore()

    auth_dep = _get_auth_dependency()
    router = build_temporal_router(
        placeholder_client,  # type: ignore[arg-type]
        auth_dependency=auth_dep,
        definition_store=definition_store,
    )
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """Liveness probe — returns 200 when process is alive."""
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz():
        """Readiness probe — returns 200 when Temporal is reachable, 503 otherwise."""
        if "client" in temporal_client_holder:
            return {"status": "ready"}
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/metrics")
    async def metrics():
        """Prometheus metrics endpoint."""
        from fastapi.responses import PlainTextResponse
        from prometheus_client import generate_latest

        return PlainTextResponse(
            generate_latest(), media_type="text/plain; charset=utf-8"
        )

    return app


class _DeferredClient:
    """Proxy that delegates to a Temporal Client set during lifespan."""

    def __init__(self, holder: dict[str, Client]) -> None:
        self._holder = holder

    def __getattr__(self, name: str):
        """Delegate attribute access to the held client."""
        return getattr(self._holder["client"], name)


app = build_temporal_app()
