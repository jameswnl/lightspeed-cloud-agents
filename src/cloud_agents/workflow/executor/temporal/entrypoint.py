"""Temporal workflow runner entrypoint.

Builds a FastAPI app with Temporal workflow endpoints. The Temporal
client and worker are created in the app lifespan and shut down on exit.

Usage: uvicorn agents.workflow.temporal_entrypoint:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.worker import Worker

from cloud_agents.runtime.tracing import init_tracing
from cloud_agents.spawner.lifetime import (
    EXPIRES_AT_LABEL_KEY,
    SPAWNED_BY_LABEL,
    await_within,
    cancel_within,
)
from cloud_agents.storage.transcript_store import TranscriptStore
from cloud_agents.workflow.core.definition_store import DefinitionStore
from cloud_agents.runtime.logging import configure_logging
from cloud_agents.workflow.executor.temporal.api import build_temporal_router
from cloud_agents.workflow.executor.temporal.worker import build_worker_config

if TYPE_CHECKING:
    from cloud_agents.spawner.base import AgentSpawner

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

# Operational margins on every sandbox's expires-at label, not enforcement:
# expiry safety comes from the per-sandbox lifetime (the enforced deadlines in
# cloud_agents.spawner.lifetime). The floor only delays how soon an expired
# orphan may be reaped.
DEFAULT_SANDBOX_MIN_LIFETIME_SECONDS = 86400
# Added to every expires-at for clock skew between replicas.
DEFAULT_SANDBOX_EXPIRY_SKEW_SECONDS = 600
# How often each replica re-runs reconcile to reap sandboxes that expired
# since startup; 0 disables the periodic reaper.
DEFAULT_SANDBOX_REAP_INTERVAL_SECONDS = 300
# Bound on each gateway call (list, delete) a reconcile run makes, so a hung
# gateway can't stall startup or the reaper.
SANDBOX_REAP_CALL_TIMEOUT_SECONDS = 30


def _non_negative_int_env(name: str, default: int) -> int:
    """Parse a non-negative integer env var, failing startup on bad values."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if value < 0:
        raise ValueError(f"{name}={value} must not be negative")
    return value


def _sandbox_min_lifetime_seconds() -> int:
    """SANDBOX_MIN_LIFETIME_SECONDS (default 86400): floor for sandbox expiry."""
    return _non_negative_int_env(
        "SANDBOX_MIN_LIFETIME_SECONDS", DEFAULT_SANDBOX_MIN_LIFETIME_SECONDS
    )


def _sandbox_expiry_skew_seconds() -> int:
    """SANDBOX_EXPIRY_SKEW_SECONDS (default 600): clock-skew margin on expiry."""
    return _non_negative_int_env("SANDBOX_EXPIRY_SKEW_SECONDS", DEFAULT_SANDBOX_EXPIRY_SKEW_SECONDS)


def _sandbox_reap_interval_seconds() -> int:
    """REAPER_INTERVAL_SECONDS (default 300): periodic reaper interval, 0 disables."""
    return _non_negative_int_env("REAPER_INTERVAL_SECONDS", DEFAULT_SANDBOX_REAP_INTERVAL_SECONDS)


def _create_spawner():
    """Create spawner based on environment config.

    Thin env-var-reading wrapper around cloud_agents.spawner.factory.build_spawner
    -- see that module for the actual per-type construction logic. OpenShellSpawner
    is the only supported ephemeral spawner (issue #198).

    Raises:
        ValueError: If WORKFLOW_SPAWNER is set to anything other than "openshell"
            or empty. This must be fail-closed, not a silent stub fallback --
            e.g. a leftover "kubernetes"/"podman" value from an unmigrated
            deploy manifest (or a typo) would otherwise silently disable
            ephemeral spawning instead of refusing to start. Also raised for
            an invalid SANDBOX_MIN_LIFETIME_SECONDS / SANDBOX_EXPIRY_SKEW_SECONDS.
    """
    if SPAWNER_TYPE == "openshell":
        from cloud_agents.spawner.factory import build_spawner

        return build_spawner(
            "openshell",
            gateway_url=os.environ.get("OPENSHELL_GATEWAY_URL", "localhost:17670"),
            workspace=os.environ.get("OPENSHELL_WORKSPACE", "default"),
            http_endpoint=os.environ.get("OPENSHELL_HTTP_ENDPOINT", ""),
            tls_ca=os.environ.get("OPENSHELL_TLS_CA", ""),
            tls_cert=os.environ.get("OPENSHELL_TLS_CERT", ""),
            tls_key=os.environ.get("OPENSHELL_TLS_KEY", ""),
            bearer_token=os.environ.get("OPENSHELL_BEARER_TOKEN", ""),
            min_lifetime_seconds=_sandbox_min_lifetime_seconds(),
            expiry_skew_seconds=_sandbox_expiry_skew_seconds(),
        )
    if SPAWNER_TYPE == "":
        logger.info("No spawner configured — sandbox activity will use stub mode")
        return None
    raise ValueError(
        f"Unknown WORKFLOW_SPAWNER={SPAWNER_TYPE!r}; expected 'openshell' or unset (stub mode)"
    )


async def reconcile_orphaned_sandboxes(
    spawner: AgentSpawner | None, now: float | None = None, *, trigger: str = "startup"
) -> None:
    """Destroy expired orphaned sandboxes left from a previous crash.

    Runs on startup and then periodically (start_sandbox_reaper()). Lists
    sandboxes labelled "spawned-by=workflow-runner" once and destroys exactly
    those whose "cloud-agents/expires-at" is in the past, whichever runner
    spawned them. Expiry is the only criterion: no runner identity can prove a
    sandbox's previous owner is gone, so an unexpired sandbox may still be in
    use by a live replica.

    Sandboxes with no expires-at label (spawned before expiry labelling
    existed, or with no enforced lifetime, e.g. CLI sessions) and sandboxes
    with a malformed one are left alone and logged.

    The list and each delete are bounded by SANDBOX_REAP_CALL_TIMEOUT_SECONDS.
    Several replicas may reconcile at once; the spawner treats deleting an
    already-deleted sandbox as success.

    Args:
        spawner: The spawner instance, or None if no spawner is configured.
        now: Current unix time; defaults to time.time(). Injectable for tests.
        trigger: "startup" or "reaper", for logs and the audit event.

    Raises:
        Exception: If listing sandboxes fails or times out (counted in
            ls_sandbox_reap_failures_total). Delete failures are logged and
            counted, not raised.
    """
    if spawner is None:
        return
    from cloud_agents.workflow.executor.temporal.metrics import (
        ls_sandbox_orphans_cleaned_total,
        ls_sandbox_reap_failures_total,
    )

    now = time.time() if now is None else now

    try:
        listed = await await_within(
            spawner.list_sandbox_labels(SPAWNED_BY_LABEL),
            SANDBOX_REAP_CALL_TIMEOUT_SECONDS,
            what="Orphaned sandbox list",
        )
    except Exception:
        ls_sandbox_reap_failures_total.labels(operation="list").inc()
        raise
    orphans = []
    no_expiry = 0
    for name, labels in listed:
        expires_at = labels.get(EXPIRES_AT_LABEL_KEY)
        if expires_at is None:
            no_expiry += 1
            continue
        try:
            expired = int(expires_at) < now
        except ValueError:
            logger.warning(
                "Leaving sandbox '%s' untouched: malformed expires-at label %r",
                name,
                expires_at,
            )
            continue
        if expired:
            orphans.append(name)
    if no_expiry:
        logger.info("Leaving %d sandbox(es) with no expires-at label untouched", no_expiry)
    cleaned = 0
    failed_names = []
    for name in orphans:
        logger.warning("Destroying orphaned sandbox '%s'", name)
        try:
            await await_within(
                spawner.destroy_sandbox(name),
                SANDBOX_REAP_CALL_TIMEOUT_SECONDS,
                what=f"Orphaned sandbox '{name}' delete",
            )
            cleaned += 1
        except NotImplementedError:
            # Never fall back to agent-name destroy(): it may not be exact.
            logger.warning(
                "Skipping orphaned sandbox '%s': spawner can't destroy by exact name", name
            )
            failed_names.append(name)
        except Exception as exc:
            logger.error("Failed to destroy orphaned sandbox '%s': %s", name, exc)
            ls_sandbox_reap_failures_total.labels(operation="delete").inc()
            failed_names.append(name)
    if orphans:
        logger.info("Cleaned up %d/%d orphaned sandbox(es) on %s", cleaned, len(orphans), trigger)
        from cloud_agents.runtime.audit import emit_audit

        ls_sandbox_orphans_cleaned_total.inc(cleaned)
        emit_audit(
            event_type="orphan_cleanup",
            workflow_id=trigger,
            details={"count": cleaned, "failed": failed_names, "total_found": len(orphans)},
        )


def start_sandbox_reaper(
    spawner: AgentSpawner | None,
    interval: float,
    *,
    clock: Callable[[], float] = time.time,
) -> asyncio.Task[None] | None:
    """Start re-running reconcile_orphaned_sandboxes() every interval seconds.

    Startup reconcile alone leaves a crashed replica's sandboxes until some
    runner restarts. Runs are single-flight: a tick that fires while the
    previous run is still in progress is skipped. Each run's gateway calls
    are bounded, and its errors are logged, never raised.

    Returns:
        The reaper task, for stop_sandbox_reaper(); None if there's no
        spawner or interval is 0 (disabled).
    """
    if spawner is None or interval == 0:
        return None
    logger.info("Sandbox reaper started (every %gs)", interval)
    return asyncio.create_task(_reap_periodically(spawner, interval, clock), name="sandbox-reaper")


async def stop_sandbox_reaper(task: asyncio.Task[None] | None) -> None:
    """Cancel the reaper (and any run in progress) on shutdown."""
    if task is not None:
        await cancel_within(task, what="Sandbox reaper")


async def _reap_periodically(
    spawner: AgentSpawner, interval: float, clock: Callable[[], float]
) -> None:
    run: asyncio.Task[None] | None = None
    try:
        while True:
            await asyncio.sleep(interval)
            if run is not None and not run.done():
                logger.warning("Skipping sandbox reaper run: previous run still in progress")
                continue
            run = asyncio.create_task(_reap_once(spawner, clock))
    finally:
        if run is not None and not run.done():
            await cancel_within(run, what="Sandbox reaper run")


async def _reap_once(spawner: AgentSpawner, clock: Callable[[], float]) -> None:
    try:
        await reconcile_orphaned_sandboxes(spawner, now=clock(), trigger="reaper")
    except Exception as exc:
        logger.error("Sandbox reaper run failed: %s", exc)


CONTENT_POLICY_PATH = os.environ.get("CONTENT_POLICY_PATH", "")


def _load_content_policy():
    """Load content policy from CONTENT_POLICY_PATH env var.

    Returns None when the env var is unset, allowing backward-compatible
    operation without any content policy enforcement.
    """
    if not CONTENT_POLICY_PATH:
        logger.info("CONTENT_POLICY_PATH not set — content policy disabled")
        return None

    from cloud_agents.workflow.security.content_policy import load_content_policy

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
    worker_config = build_worker_config(spawner=spawner, transcript_store=transcript_store)
    reap_interval = _sandbox_reap_interval_seconds()
    temporal_client_holder: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Connect Temporal client and start worker on startup."""
        await reconcile_orphaned_sandboxes(spawner)
        reaper = start_sandbox_reaper(spawner, reap_interval)

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
                logger.info("Temporal worker started on queue '%s'", worker_config.task_queue)
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
            await stop_sandbox_reaper(reaper)
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

    from cloud_agents.runtime.middleware import ContentSizeLimitMiddleware

    max_body = int(os.environ.get("MAX_REQUEST_BODY_BYTES", "1048576"))
    app.add_middleware(ContentSizeLimitMiddleware, max_content_size=max_body)

    rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "false").lower() == "true"
    if rate_limit_enabled:
        from cloud_agents.runtime.rate_limiter import RateLimitMiddleware

        rate = float(os.environ.get("RATE_LIMIT_RATE", "10"))
        burst = int(os.environ.get("RATE_LIMIT_BURST", "20"))
        app.add_middleware(RateLimitMiddleware, rate=rate, burst=burst)
        logger.info("Rate limiting enabled (rate=%.1f/s, burst=%d)", rate, burst)

    # Register built-in tools before building the API router
    from cloud_agents.tools import load_builtin_tools

    load_builtin_tools()

    # Load custom tools module if configured
    tools_module = os.environ.get("CLOUD_AGENTS_TOOLS_MODULE")
    if tools_module:
        from cloud_agents.workflow.executor.step.tools import load_tools_module

        load_tools_module(tools_module)

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
        from cloud_agents.workflow.triggers.alert_trigger import (
            AlertTriggerConfig,
            build_alert_router,
        )

        alert_config = AlertTriggerConfig(
            workflow_name_label=os.environ.get(
                "ALERT_TRIGGER_WORKFLOW_LABEL", "cloud_agents_workflow"
            ),
            default_workflow=os.environ.get("ALERT_TRIGGER_DEFAULT_WORKFLOW") or None,
            dedup_window_seconds=int(os.environ.get("ALERT_TRIGGER_DEDUP_WINDOW", "300")),
            fire_on_resolved=os.environ.get("ALERT_TRIGGER_FIRE_ON_RESOLVED", "false").lower()
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

    schedule_trigger_enabled = os.environ.get("SCHEDULE_TRIGGER_ENABLED", "false").lower() == "true"
    if schedule_trigger_enabled:
        from cloud_agents.workflow.triggers.schedule_trigger import build_schedule_router

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

        return PlainTextResponse(generate_latest(), media_type="text/plain; charset=utf-8")

    return app


class _DeferredClient:
    """Proxy that delegates to a Temporal Client set during lifespan."""

    def __init__(self, holder: dict[str, Client]) -> None:
        self._holder = holder

    def __getattr__(self, name: str):
        """Delegate attribute access to the held client."""
        return getattr(self._holder["client"], name)


app = build_temporal_app()
