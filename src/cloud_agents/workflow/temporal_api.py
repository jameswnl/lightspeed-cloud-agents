"""Temporal workflow API endpoints.

Provides REST endpoints for starting, approving, querying, and
cancelling Temporal-backed agent workflows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from temporalio.client import Client

from agents.workflow.definition_store import DefinitionStore
from agents.workflow.temporal_models import ProviderConfig, WorkflowInput
from agents.workflow.temporal_worker import DEFAULT_TASK_QUEUE
from agents.workflow.temporal_workflow import AgentWorkflow

logger = logging.getLogger(__name__)


class RunWorkflowRequest(BaseModel):
    """Request body for starting a workflow."""

    workflow_name: str | None = None
    definition: dict[str, Any] | None = None
    input_prompt: str | None = None
    provider: ProviderConfig | None = None
    sandbox_image: str = "quay.io/openshift-lightspeed/lightspeed-agentic-sandbox:latest"
    skills_image: str | None = None
    skills_paths: list[str] | None = None


class ApproveRequest(BaseModel):
    """Request body for sending an approval signal."""

    step_name: str
    decision: str
    selected_option_id: str | None = None


def build_temporal_router(
    temporal_client: Client,
    auth_dependency: Optional[Any] = None,
    definition_store: Optional[DefinitionStore] = None,
) -> APIRouter:
    """Build FastAPI router with Temporal workflow endpoints.

    Parameters:
        temporal_client: Connected Temporal client instance.
        auth_dependency: Optional FastAPI auth dependency. All endpoints
            require authentication when provided.
        definition_store: Optional store for workflow-name resolution.

    Returns:
        APIRouter with workflow endpoints.
    """
    dependencies = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(
        prefix="/v1/workflows", tags=["workflows"], dependencies=dependencies,
    )

    @router.post("/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_workflow(request: RunWorkflowRequest) -> dict[str, str]:
        """Start a new workflow execution."""
        definition = request.definition

        if request.workflow_name and not definition:
            if not definition_store:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="workflow_name requires a definition store",
                )
            stored = await definition_store.get(request.workflow_name)
            if not stored:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workflow '{request.workflow_name}' not found",
                )
            definition = stored.definition.model_dump()
            provider = request.provider or (
                ProviderConfig(**stored.definition.provider.model_dump())
                if stored.definition.provider else None
            )
        else:
            provider = request.provider

        if not definition:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either definition or workflow_name is required",
            )
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provider configuration is required",
            )

        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        workflow_input = WorkflowInput(
            definition=definition,
            input_prompt=request.input_prompt,
            workflow_id=workflow_id,
            provider=provider,
            sandbox_image=request.sandbox_image,
            skills_image=request.skills_image,
            skills_paths=request.skills_paths,
        )

        await temporal_client.start_workflow(
            AgentWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=DEFAULT_TASK_QUEUE,
        )

        return {"workflow_id": workflow_id}

    if definition_store:
        @router.post("/definitions", status_code=status.HTTP_201_CREATED)
        async def submit_definition(body: dict[str, Any]) -> dict[str, Any]:
            """Submit a workflow definition to the store."""
            from agents.workflow.definition import WorkflowDefinition
            defn = WorkflowDefinition.model_validate(body)
            stored = await definition_store.save(defn)
            return {"name": stored.name, "version": stored.version}

        @router.get("/definitions")
        async def list_definitions() -> list[dict[str, Any]]:
            """List all active workflow definitions."""
            defs = await definition_store.list_all()
            return [{"name": d.name, "version": d.version} for d in defs]

        @router.get("/definitions/{name}")
        async def get_definition(name: str) -> dict[str, Any]:
            """Get a workflow definition by name."""
            stored = await definition_store.get(name)
            if not stored:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Definition '{name}' not found",
                )
            return {"name": stored.name, "version": stored.version,
                    "definition": stored.definition.model_dump()}

    @router.post("/{workflow_id}/approve")
    async def approve_workflow(
        workflow_id: str, request: ApproveRequest,
    ) -> dict[str, str]:
        """Send an approval signal to a running workflow."""
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            AgentWorkflow.approve,
            args=[request.step_name, request.decision, request.selected_option_id],
        )
        return {"status": "signal_sent"}

    @router.get("/{workflow_id}")
    async def get_workflow_status(workflow_id: str) -> dict[str, Any]:
        """Query the current workflow status."""
        handle = temporal_client.get_workflow_handle(workflow_id)
        status_result = await handle.query(AgentWorkflow.get_status)
        if hasattr(status_result, "model_dump"):
            return status_result.model_dump()
        return {"steps": {}, "events": []}

    @router.get("/{workflow_id}/events")
    async def get_workflow_events(workflow_id: str) -> StreamingResponse:
        """Stream workflow events via SSE, polling status every second."""
        handle = temporal_client.get_workflow_handle(workflow_id)
        seen_count = 0

        async def event_generator():
            nonlocal seen_count
            while True:
                try:
                    result = await handle.query(AgentWorkflow.get_status)
                    events = result.events if hasattr(result, "events") else []
                    for event in events[seen_count:]:
                        data = event.model_dump() if hasattr(event, "model_dump") else event
                        yield f"data: {data}\n\n"
                    seen_count = len(events)

                    steps = result.steps if hasattr(result, "steps") else {}
                    all_terminal = steps and all(
                        s.status in ("completed", "failed", "skipped", "denied", "escalated")
                        for s in steps.values()
                    )
                    if all_terminal:
                        yield "data: {\"type\": \"workflow.completed\"}\n\n"
                        break
                except Exception:
                    yield "data: {\"type\": \"workflow.error\"}\n\n"
                    break

                await asyncio.sleep(1)

        return StreamingResponse(
            event_generator(), media_type="text/event-stream",
        )

    @router.post("/{workflow_id}/cancel")
    async def cancel_workflow(workflow_id: str) -> dict[str, str]:
        """Cancel a running workflow."""
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
        return {"status": "cancelled"}

    return router
