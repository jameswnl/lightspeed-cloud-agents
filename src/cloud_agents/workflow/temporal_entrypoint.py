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

from cloud_agents.runtime.tracing import init_tracing
from cloud_agents.storage.transcript_store import TranscriptStore
from cloud_agents.workflow.definition_store import DefinitionStore
from cloud_agents.workflow.structured_logging import configure_logging
from cloud_agents.workflow.temporal_api import build_temporal_router
from cloud_agents.workflow.temporal_worker import build_worker_config

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
        from cloud_agents.spawner.kubernetes_spawner import KubernetesSpawner

        namespace = os.environ.get("SPAWNER_NAMESPACE", "default")
        service_account = os.environ.get("SPAWNER_SERVICE_ACCOUNT", "workflow-runner")
        logger.info("Using KubernetesSpawner (namespace=%s)", namespace)
        return KubernetesSpawner(namespace=namespace, service_account=service_account)
    if SPAWNER_TYPE == "podman":
        from cloud_agents.spawner.podman_spawner import PodmanSpawner

        network = os.environ.get("SPAWNER_NETWORK", "cloud-agents")
        logger.info("Using PodmanSpawner (network=%s)", network)
        return PodmanSpawner(network=network)
    if SPAWNER_TYPE == "openshell":
        from cloud_agents.spawner.openshell_spawner import OpenShellSpawner
        from openshell import SandboxClient

        gateway_url = os.environ.get("OPENSHELL_GATEWAY_URL", "http://localhost:17670")
        podman_cli = os.environ.get("OPENSHELL_PODMAN_CLI")
        client = SandboxClient(endpoint=gateway_url)
        logger.info("Using OpenShellSpawner (gateway=%s)", gateway_url)
        return OpenShellSpawner(openshell_client=client, podman_cli=podman_cli)
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
    cleaned = 0
    failed_names = []
    for name in orphans:
        logger.warning("Destroying orphaned sandbox '%s'", name)
        try:
            await spawner.destroy(name)
            cleaned += 1
        except Exception as exc:
            logger.error("Failed to destroy orphaned sandbox '%s': %s", name, exc)
            failed_names.append(name)
    if orphans:
        logger.info("Cleaned up %d/%d orphaned sandbox(es) on startup", cleaned, len(orphans))
        from cloud_agents.workflow.audit import emit_audit
        from cloud_agents.workflow.temporal_metrics import ls_sandbox_orphans_cleaned_total

        ls_sandbox_orphans_cleaned_total.inc(cleaned)
        emit_audit(
            event_type="orphan_cleanup",
            workflow_id="startup",
            details={"count": cleaned, "failed": failed_names, "total_found": len(orphans)},
        )


CONTENT_POLICY_PATH = os.environ.get("CONTENT_POLICY_PATH", "")


def _load_content_policy():
    """Load content policy from CONTENT_POLICY_PATH env var.

    Returns None when the env var is unset, allowing backward-compatible
    operation without any content policy enforcement.
    """
    if not CONTENT_POLICY_PATH:
        logger.info("CONTENT_POLICY_PATH not set — content policy disabled")
        return None

    from cloud_agents.workflow.content_policy import load_content_policy

    policy = load_content_policy(CONTENT_POLICY_PATH)
    logger.info("Content policy loaded from %s", CONTENT_POLICY_PATH)
    return policy


AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"


def _get_auth_dependency():
    """Build auth dependency from environment configuration.

    Returns None when AUTH_REQUIRED=false. Fails closed when true.

    For shared_secret mode, returns a FastAPI dependency function (closure)
    that validates bearer tokens against the configured token list.
    For sa_token mode, returns the TokenReviewAuthMiddleware class.
    """
    if not AUTH_REQUIRED:
        logger.info("AUTH_REQUIRED=false — endpoints unauthenticated")
        return None

    from cloud_agents.runtime.auth import (
        TokenReviewAuthMiddleware,
        create_bearer_auth_dependency,
        get_api_tokens,
        get_auth_mode,
    )

    mode = get_auth_mode()
    tokens = get_api_tokens()
    if not tokens:
        raise RuntimeError(
            "AUTH_REQUIRED=true but no tokens configured. "
            "Set AGENT_API_TOKENS or AGENT_API_TOKEN. "
            "Refusing to start with unauthenticated workflow endpoints."
        )

    if mode == "sa_token":
        logger.info("Using TokenReview auth middleware")
        return TokenReviewAuthMiddleware
    logger.info("Using Bearer auth with %d configured token(s)", len(tokens))
    return create_bearer_auth_dependency(tokens)


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
    transcript_store = TranscriptStore.from_env()
    if transcript_store is not None:
        logger.info("Transcript store configured (TRANSCRIPT_DB_URL set)")
    else:
        logger.info("Transcript store disabled (TRANSCRIPT_DB_URL not set)")
    worker_config = build_worker_config(
        spawner=spawner, transcript_store=transcript_store
    )
    temporal_client_holder: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Connect Temporal client and start worker on startup."""
        await reconcile_orphaned_sandboxes(spawner)

        # Connect transcript store (best-effort: failures are non-fatal)
        if transcript_store is not None:
            try:
                await transcript_store.connect()
            except Exception as exc:
                logger.warning(
                    "Failed to connect transcript store: %s. "
                    "Transcripts will fall back to workflow query state.",
                    exc,
                )

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
        finally:
            # Close transcript store on shutdown
            if transcript_store is not None:
                try:
                    await transcript_store.close()
                except Exception:
                    logger.debug("Error closing transcript store", exc_info=True)

    app = FastAPI(title="Cloud Agents Workflow Runner (Temporal)", lifespan=lifespan)

    cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cors_origins.split(",")],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    from cloud_agents.workflow.middleware import ContentSizeLimitMiddleware

    max_body = int(os.environ.get("MAX_REQUEST_BODY_BYTES", "1048576"))
    app.add_middleware(ContentSizeLimitMiddleware, max_content_size=max_body)

    rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "false").lower() == "true"
    if rate_limit_enabled:
        from cloud_agents.workflow.rate_limiter import RateLimitMiddleware

        rate = float(os.environ.get("RATE_LIMIT_RATE", "10"))
        burst = int(os.environ.get("RATE_LIMIT_BURST", "20"))
        app.add_middleware(RateLimitMiddleware, rate=rate, burst=burst)
        logger.info("Rate limiting enabled (rate=%.1f/s, burst=%d)", rate, burst)

    placeholder_client = _DeferredClient(temporal_client_holder)
    definition_store = DefinitionStore()

    auth_dep = _get_auth_dependency()
    content_policy = _load_content_policy()
    router = build_temporal_router(
        placeholder_client,  # type: ignore[arg-type]
        auth_dependency=auth_dep,
        definition_store=definition_store,
        content_policy=content_policy,
        transcript_store=transcript_store,
    )
    app.include_router(router)

    alert_trigger_enabled = os.environ.get("ALERT_TRIGGER_ENABLED", "false").lower() == "true"
    if alert_trigger_enabled:
        from cloud_agents.workflow.alert_trigger import (
            AlertTriggerConfig,
            build_alert_router,
        )

        alert_config = AlertTriggerConfig(
            workflow_name_label=os.environ.get(
                "ALERT_TRIGGER_WORKFLOW_LABEL", "cloud_agents_workflow"
            ),
            default_workflow=os.environ.get("ALERT_TRIGGER_DEFAULT_WORKFLOW") or None,
            dedup_window_seconds=int(os.environ.get("ALERT_TRIGGER_DEDUP_WINDOW", "300")),
            fire_on_resolved=os.environ.get(
                "ALERT_TRIGGER_FIRE_ON_RESOLVED", "false"
            ).lower()
            == "true",
        )
        alert_router = build_alert_router(
            temporal_client=placeholder_client,  # type: ignore[arg-type]
            definition_store=definition_store,
            config=alert_config,
            auth_dependency=auth_dep,
            authorizer=None,
            content_policy=content_policy,
        )
        app.include_router(alert_router)
        logger.info("Alert trigger enabled (label=%s)", alert_config.workflow_name_label)

    schedule_trigger_enabled = (
        os.environ.get("SCHEDULE_TRIGGER_ENABLED", "false").lower() == "true"
    )
    if schedule_trigger_enabled:
        from cloud_agents.workflow.schedule_trigger import build_schedule_router

        schedule_router = build_schedule_router(
            temporal_client=placeholder_client,  # type: ignore[arg-type]
            definition_store=definition_store,
            auth_dependency=auth_dep,
            content_policy=content_policy,
        )
        app.include_router(schedule_router)
        logger.info("Schedule trigger enabled")

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
