"""Agent runtime HTTP server.

Provides a FastAPI application with /v1/run, /v1/runs/{run_id}, /healthz,
/livez, and /metrics endpoints for running Pydantic AI agents in agent pods.

Supports sync mode (default) and async mode (via Prefer: respond-async header).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import generate_latest

from agents.models import AgentRunRequest, AgentRunResponse, RunState
from agents.runtime.auth import BearerAuthMiddleware, get_api_token
from agents.runtime.correlation import validate_correlation_id
from agents.runtime.metrics import ls_agent_run_duration_seconds, ls_agent_runs_total
from agents.runtime.run_store import RunStore
from agents.runtime.tracing import extract_traceparent, get_tracer, set_span_error

logger = logging.getLogger(__name__)

AgentRunner = Callable[[AgentRunRequest], Awaitable[AgentRunResponse]]

DEFAULT_RUN_TIMEOUT = 600.0


def create_app(
    *,
    agent_runner: AgentRunner,
    agent_name: str,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
) -> FastAPI:
    """Create a FastAPI app for an agent pod.

    Args:
        agent_runner: Async callable that processes an AgentRunRequest
            and returns an AgentRunResponse.
        agent_name: Identifier for this agent (included in health and responses).
        run_timeout: Maximum seconds for a single agent run before cancellation.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title=f"Agent: {agent_name}")
    from agents.runtime.auth import get_auth_mode, TokenReviewAuthMiddleware
    auth_mode = get_auth_mode()
    if auth_mode == "sa_token":
        app.add_middleware(TokenReviewAuthMiddleware)
    else:
        api_token = get_api_token()
        if api_token:
            app.add_middleware(BearerAuthMiddleware, token=api_token)
    app.state.agent_runner = agent_runner
    app.state.agent_name = agent_name
    app.state.run_store = RunStore()
    app.state.run_timeout = run_timeout
    app.state.last_heartbeat = time.monotonic()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Readiness check."""
        return {"status": "ready", "agent_name": agent_name}

    @app.get("/livez")
    async def livez() -> Any:
        """Liveness check — detects hung agents."""
        elapsed = time.monotonic() - app.state.last_heartbeat
        max_stale = 2 * app.state.run_timeout
        if elapsed > max_stale:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "stale",
                    "agent_name": agent_name,
                    "seconds_since_heartbeat": round(elapsed, 1),
                },
            )
        return {"status": "alive", "agent_name": agent_name}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus metrics endpoint (internal-only)."""
        return PlainTextResponse(
            content=generate_latest().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/v1/run")
    async def run(body: AgentRunRequest, request: Request) -> Any:
        """Run the agent with the given prompt."""
        correlation_id = validate_correlation_id(
            (body.context or {}).get("correlation_id")
        )
        if body.context is None:
            body.context = {}
        body.context["correlation_id"] = correlation_id

        incoming_headers = dict(request.headers)
        trace_ctx = extract_traceparent(incoming_headers)

        prefer = request.headers.get("Prefer", "")
        if prefer == "respond-async":
            response = await _handle_async_run(body, correlation_id, trace_ctx)
        else:
            response = await _handle_sync_run(body, correlation_id, trace_ctx)

        if isinstance(response, JSONResponse):
            response.headers["X-Correlation-ID"] = correlation_id
        elif isinstance(response, AgentRunResponse):
            return JSONResponse(
                content=response.model_dump(mode="json"),
                headers={"X-Correlation-ID": correlation_id},
            )
        return response

    _tracer = get_tracer("agents.runtime.server")

    async def _handle_sync_run(
        body: AgentRunRequest, correlation_id: str, trace_ctx: Any = None
    ) -> AgentRunResponse:
        """Synchronous run — blocks until completion, with timeout."""
        app.state.last_heartbeat = time.monotonic()
        start_time = time.monotonic()
        logger.info(
            "Starting sync run",
            extra={"agent_name": agent_name, "correlation_id": correlation_id},
        )
        with _tracer.start_as_current_span(
            f"agent.run.{agent_name}", context=trace_ctx
        ) as span:
            span.set_attribute("agent.name", agent_name)
            span.set_attribute("correlation.id", correlation_id)
            try:
                result = await asyncio.wait_for(
                    app.state.agent_runner(body),
                    timeout=app.state.run_timeout,
                )
                duration = time.monotonic() - start_time
                if result.success:
                    ls_agent_runs_total.labels(agent_name=agent_name, status="success").inc()
                    span.set_attribute("agent.run.status", "success")
                else:
                    ls_agent_runs_total.labels(agent_name=agent_name, status="error").inc()
                    span.set_attribute("agent.run.status", "error")
                ls_agent_run_duration_seconds.labels(agent_name=agent_name).observe(duration)
                return result
            except asyncio.TimeoutError as exc:
                ls_agent_runs_total.labels(agent_name=agent_name, status="timeout").inc()
                set_span_error(span, exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Agent run timed out after {app.state.run_timeout}s",
                ) from exc
            except Exception as exc:
                ls_agent_runs_total.labels(agent_name=agent_name, status="error").inc()
                set_span_error(span, exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Agent run failed: {type(exc).__name__}: {exc}",
                ) from exc
            finally:
                app.state.last_heartbeat = time.monotonic()

    async def _handle_async_run(
        body: AgentRunRequest, correlation_id: str, trace_ctx: Any = None
    ) -> JSONResponse:
        """Asynchronous run — returns 202 immediately, runs in background."""
        store: RunStore = app.state.run_store
        state = await store.create_run()

        async def _run_in_background(run_id: str) -> None:
            app.state.last_heartbeat = time.monotonic()
            start_time = time.monotonic()
            logger.info(
                "Starting async run %s",
                run_id,
                extra={"agent_name": agent_name, "correlation_id": correlation_id},
            )
            span = _tracer.start_span(
                f"agent.run.async.{agent_name}", context=trace_ctx,
            )
            span.set_attribute("agent.name", agent_name)
            span.set_attribute("correlation.id", correlation_id)
            span.set_attribute("run.id", run_id)
            try:
                result = await asyncio.wait_for(
                    app.state.agent_runner(body),
                    timeout=app.state.run_timeout,
                )
                duration = time.monotonic() - start_time
                if result.success:
                    ls_agent_runs_total.labels(agent_name=agent_name, status="success").inc()
                    ls_agent_run_duration_seconds.labels(agent_name=agent_name).observe(duration)
                    await store.complete_run(run_id, result)
                    from agents.runtime.callback import get_callback
                    cb = get_callback()
                    if cb:
                        await cb.post_result("completed", output=result.output)
                else:
                    ls_agent_runs_total.labels(agent_name=agent_name, status="error").inc()
                    await store.fail_run(run_id, result)
                    from agents.runtime.callback import get_callback
                    cb = get_callback()
                    if cb:
                        await cb.post_result("failed", error=result.error)
            except asyncio.TimeoutError as exc:
                ls_agent_runs_total.labels(agent_name=agent_name, status="timeout").inc()
                set_span_error(span, exc)
                error_response = AgentRunResponse(
                    output={},
                    output_type="error",
                    usage={"input_tokens": 0, "output_tokens": 0},
                    agent_name=agent_name,
                    success=False,
                    error=f"Agent run timed out after {app.state.run_timeout}s",
                )
                await store.fail_run(run_id, error_response)
                from agents.runtime.callback import get_callback
                cb = get_callback()
                if cb:
                    await cb.post_result("failed", error=error_response.error)
            except Exception as exc:
                ls_agent_runs_total.labels(agent_name=agent_name, status="error").inc()
                set_span_error(span, exc)
                error_response = AgentRunResponse(
                    output={},
                    output_type="error",
                    usage={"input_tokens": 0, "output_tokens": 0},
                    agent_name=agent_name,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                await store.fail_run(run_id, error_response)
                from agents.runtime.callback import get_callback
                cb = get_callback()
                if cb:
                    await cb.post_result("failed", error=error_response.error)
            finally:
                span.end()
                app.state.last_heartbeat = time.monotonic()

        asyncio.create_task(_run_in_background(state.run_id))
        return JSONResponse(
            status_code=202,
            content={"run_id": state.run_id, "status": "running"},
        )

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> RunState:
        """Poll the status of an async run."""
        store: RunStore = app.state.run_store
        state = await store.get_run(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        return state

    return app
