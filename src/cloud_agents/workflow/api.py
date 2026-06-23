"""Workflow HTTP API — endpoints for running and managing workflows.

Provides run, poll, approve, and list endpoints for multi-step workflows.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agents.runtime.auth import BearerAuthMiddleware, get_api_token
from agents.workflow.executor import WorkflowExecutor
from agents.workflow.state import WorkflowState


def create_workflow_app(
    executor: WorkflowExecutor,
    workflow_name: str,
) -> FastAPI:
    """Create a FastAPI app for the workflow runner.

    Args:
        executor: The workflow executor instance.
        workflow_name: Name for healthz/display.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title=f"Workflow: {workflow_name}")
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

        Runs the workflow inline. For workflows with approval steps,
        returns when the workflow pauses. For fully automatic workflows,
        returns when the workflow completes.
        """
        body = await request.json() if await request.body() else {}
        input_prompt = body.get("input_prompt")

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

    return app
