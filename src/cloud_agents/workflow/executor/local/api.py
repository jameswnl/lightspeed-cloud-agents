"""FastAPI router for the local workflow runner.

Provides the same /run, /approve, /cancel, /{id} endpoints as the
Temporal API but dispatches to LocalWorkflowRunner instead.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from cloud_agents.workflow.executor.base import ApprovalDecision
from cloud_agents.workflow.executor.local.executor import LocalWorkflowRunner

logger = logging.getLogger(__name__)


class RunWorkflowRequest(BaseModel):
    """Request body for starting a workflow."""

    workflow_name: Optional[str] = None
    definition: Optional[dict[str, Any]] = None
    input_prompt: Optional[str] = None
    provider: Optional[dict[str, Any]] = None
    sandbox_image: str = "sandbox:latest"
    skills_image: Optional[str] = None
    skills_paths: Optional[list[str]] = None
    workflow_id: Optional[str] = None
    mcp_servers: Optional[list[dict[str, Any]]] = None
    approval_policy: Optional[dict[str, Any]] = None


class ApproveRequest(BaseModel):
    """Request body for sending an approval signal."""

    step_name: str
    decision: str
    selected_option_id: Optional[str] = None


def build_local_router(
    executor: LocalWorkflowRunner,
    get_caller_identity: Any = None,
    content_policy: Any = None,
) -> APIRouter:
    """Build FastAPI router for the local workflow runner.

    Parameters:
        executor: LocalWorkflowRunner instance.
        get_caller_identity: Optional auth dependency.
        content_policy: Optional content policy.

    Returns:
        APIRouter with workflow endpoints.
    """
    deps = [Depends(get_caller_identity)] if get_caller_identity else []
    router = APIRouter(tags=["workflows"], dependencies=deps)

    @router.post("/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_workflow(request: RunWorkflowRequest) -> dict[str, str]:
        """Start a new workflow execution."""
        if not request.definition:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workflow definition is required",
            )

        if not request.provider:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provider configuration is required",
            )

        if content_policy:
            from cloud_agents.workflow.core.validation import validate_definition

            errors = validate_definition(request.definition, content_policy)
            if errors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"validation_errors": errors},
                )

        input_data: dict[str, Any] = {
            "definition": request.definition,
            "provider": request.provider,
            "sandbox_image": request.sandbox_image,
            "skills_image": request.skills_image,
            "skills_paths": request.skills_paths,
            "mcp_servers": request.mcp_servers,
            "approval_policy": request.approval_policy,
            "input_prompt": request.input_prompt,
        }
        if request.workflow_id:
            input_data["workflow_id"] = request.workflow_id

        try:
            workflow_id = await executor.start(input_data)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        return {"workflow_id": workflow_id}

    @router.post("/{workflow_id}/approve")
    async def approve_workflow(
        workflow_id: str, request: ApproveRequest
    ) -> dict[str, str]:
        """Send an approval signal to a paused workflow."""
        try:
            await executor.approve(
                workflow_id,
                ApprovalDecision(
                    step_name=request.step_name,
                    decision=request.decision,
                    selected_option_id=request.selected_option_id,
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return {"status": "signal_sent"}

    @router.get("/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        """Get workflow status."""
        try:
            wf_status = await executor.get_status(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {
            "workflow_id": wf_status.workflow_id,
            "status": wf_status.status,
            "is_terminal": wf_status.is_terminal,
            "steps": wf_status.steps,
            "events": wf_status.events,
        }

    @router.post("/{workflow_id}/cancel")
    async def cancel_workflow(workflow_id: str) -> dict[str, str]:
        """Cancel a running workflow."""
        try:
            await executor.cancel(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {"status": "cancelled"}

    @router.get("/{workflow_id}/handoff")
    async def get_handoff(workflow_id: str) -> dict[str, Any]:
        """Get escalation handoff context."""
        try:
            context = await executor.get_workflow_context(workflow_id)
            transcripts = await executor.get_step_transcripts(workflow_id)
            wf_status = await executor.get_status(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {
            "workflow_id": workflow_id,
            "status": wf_status.status,
            "steps": wf_status.steps,
            "context": context,
            "transcripts": transcripts,
        }

    return router
