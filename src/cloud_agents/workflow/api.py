"""Workflow HTTP API — endpoints for running and managing workflows.

Provides run, poll, approve, list, and SSE streaming endpoints for
multi-step workflows.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agents.runtime.auth import BearerAuthMiddleware, get_api_token
from agents.workflow.definition import WorkflowDefinition
from agents.workflow.definition_store import DefinitionStore
from agents.workflow.events import WorkflowEvent
from agents.workflow.executor import WorkflowExecutor
from agents.workflow.state import WorkflowState

LifespanType = Any


def create_workflow_app(
    executor: WorkflowExecutor,
    workflow_name: str,
    lifespan: Optional[LifespanType] = None,
) -> FastAPI:
    """Create a FastAPI app for the workflow runner.

    Args:
        executor: The workflow executor instance.
        workflow_name: Name for healthz/display.
        lifespan: Optional async context manager for startup/shutdown.

    Returns:
        Configured FastAPI application.
    """
    kwargs: dict[str, Any] = {"title": f"Workflow: {workflow_name}"}
    if lifespan is not None:
        kwargs["lifespan"] = lifespan
    app = FastAPI(**kwargs)
    api_token = get_api_token()
    if api_token:
        app.add_middleware(BearerAuthMiddleware, token=api_token)
    app.state.executor = executor

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Readiness check."""
        return {"status": "ready", "workflow": workflow_name}

    @app.post("/v1/workflows/run")
    async def run_workflow(request: Request) -> JSONResponse:
        """Start a new workflow execution.

        Accepts optional workflow_name to run a submitted definition,
        or runs the default loaded workflow if no name is given.
        """
        body = await request.json() if await request.body() else {}
        input_prompt = body.get("input_prompt")
        workflow_name = body.get("workflow_name")

        if workflow_name:
            stored = await definition_store.get(workflow_name)
            if stored is None:
                raise HTTPException(status_code=404, detail=f"Workflow '{workflow_name}' not found")
            from agents.workflow.executor import WorkflowExecutor
            run_executor = WorkflowExecutor(
                stored.definition,
                app.state.executor._registry,
                persistence=app.state.executor._persistence,
                approval_policy=app.state.executor._approval_policy,
                spawner=app.state.executor._spawner,
                agent_image=app.state.executor._agent_image,
                advisory=app.state.executor._advisory,
            )
            state = await run_executor.run(input_prompt=input_prompt)
        else:
            state = await app.state.executor.run(input_prompt=input_prompt)
        return JSONResponse(
            status_code=202,
            content={
                "workflow_id": state.workflow_id,
                "status": state.status,
            },
        )

    @app.get("/v1/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> Any:
        """Get workflow state by ID."""
        state = await app.state.executor.get_state(workflow_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return state

    @app.post("/v1/workflows/{workflow_id}/approve")
    async def approve_workflow(workflow_id: str, request: Request) -> Any:
        """Approve or reject a paused workflow step.

        Auth is handled by the BearerAuthMiddleware — no per-endpoint check needed.
        """
        body = await request.json()
        approved = body.get("approved", False)

        try:
            state = await app.state.executor.resume(workflow_id, approved=approved)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return state

    @app.get("/v1/workflows")
    async def list_workflows() -> list[dict[str, Any]]:
        """List all active workflows."""
        states = await app.state.executor.list_workflows()
        return [
            {
                "workflow_id": s.workflow_id,
                "workflow_name": s.workflow_name,
                "status": s.status,
                "current_step": s.current_step,
            }
            for s in states
        ]

    # --- Definition CRUD ---

    definition_store = DefinitionStore()
    app.state.definition_store = definition_store

    @app.post("/v1/workflows/definitions")
    async def submit_definition(request: Request) -> JSONResponse:
        """Submit a workflow definition YAML. Creates a new version."""
        import yaml as yaml_mod
        body = await request.body()
        try:
            data = yaml_mod.safe_load(body)
            defn = WorkflowDefinition.model_validate(data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid workflow definition: {exc}") from exc

        stored = await definition_store.save(defn)
        return JSONResponse(
            status_code=201,
            content={"name": stored.name, "version": stored.version},
        )

    @app.get("/v1/workflows/definitions")
    async def list_definitions() -> list[dict[str, Any]]:
        """List all active workflow definitions."""
        defs = await definition_store.list_all()
        return [{"name": d.name, "version": d.version, "created_at": d.created_at} for d in defs]

    @app.get("/v1/workflows/definitions/{name}")
    async def get_definition(name: str) -> Any:
        """Get the latest version of a workflow definition."""
        stored = await definition_store.get(name)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"Definition '{name}' not found")
        return stored.model_dump(mode="json")

    @app.delete("/v1/workflows/definitions/{name}")
    async def delete_definition(name: str) -> dict[str, str]:
        """Soft-delete a workflow definition."""
        deleted = await definition_store.delete(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Definition '{name}' not found")
        return {"status": "deleted", "name": name}

    # --- Streaming ---

    @app.post("/v1/workflows/run/stream")
    async def run_workflow_stream(request: Request) -> StreamingResponse:
        """Start a workflow and stream progress events via SSE."""
        body = await request.json() if await request.body() else {}
        input_prompt = body.get("input_prompt")

        event_queue: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()

        async def callback(event: WorkflowEvent) -> None:
            await event_queue.put(event)

        executor_with_events = WorkflowExecutor(
            app.state.executor._definition,
            app.state.executor._registry,
            client_factory=app.state.executor._client_factory,
            persistence=app.state.executor._persistence,
            approval_policy=app.state.executor._approval_policy,
            spawner=app.state.executor._spawner,
            agent_image=app.state.executor._agent_image,
            advisory=app.state.executor._advisory,
            event_callback=callback,
        )

        async def run_and_signal() -> None:
            try:
                await executor_with_events.run(input_prompt=input_prompt)
            finally:
                await event_queue.put(None)

        asyncio.create_task(run_and_signal())

        async def event_generator() -> AsyncGenerator[str, None]:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event.to_sse()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
        )

    return app
