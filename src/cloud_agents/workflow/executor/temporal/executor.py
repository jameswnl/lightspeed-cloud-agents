"""Temporal-backed workflow runner.

Wraps the Temporal Client behind the WorkflowRunner interface.
All Temporal-specific calls (start_workflow, signal, query, cancel,
describe) are isolated here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError

from cloud_agents.workflow.executor.base import (
    ApprovalDecision,
    WorkflowRunner,
    WorkflowStatus,
)
from cloud_agents.workflow.executor.temporal.workflow import AgentWorkflow

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
}


def _to_dict(result: Any) -> dict[str, Any]:
    """Convert a query result to a dict, handling Pydantic models."""
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {}


class TemporalWorkflowRunner(WorkflowRunner):
    """Workflow runner backed by Temporal.

    Wraps Temporal Client calls behind the WorkflowRunner interface
    so the API layer doesn't depend on Temporal directly.
    """

    def __init__(self, client: Client) -> None:
        """Initialize with a Temporal Client.

        Parameters:
            client: Connected Temporal Client instance.
        """
        self._client = client

    async def start(self, workflow_input: dict[str, Any]) -> str:
        """Start a workflow via Temporal."""
        workflow_id = workflow_input.get("workflow_id") or f"wf-{uuid.uuid4().hex[:12]}"

        try:
            await self._client.start_workflow(
                AgentWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue="cloud-agents",
            )
        except RPCError as exc:
            if "already started" in str(exc).lower():
                raise ValueError(f"Workflow '{workflow_id}' already exists") from exc
            raise

        return workflow_id

    async def approve(
        self,
        workflow_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Send an approval signal to a Temporal workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(
                AgentWorkflow.approve,
                args=[
                    decision.step_name,
                    decision.decision,
                    decision.selected_option_id,
                    decision.approver,
                    "",
                ],
            )
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise

    async def cancel(self, workflow_id: str) -> None:
        """Cancel a Temporal workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            await handle.cancel()
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise

    async def get_status(self, workflow_id: str) -> WorkflowStatus:
        """Query workflow status from Temporal."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            result = await handle.query(AgentWorkflow.get_status)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise

        data = _to_dict(result)
        steps = data.get("steps", {})
        events = data.get("events", [])

        desc = await handle.describe()
        is_terminal = desc.status in _TERMINAL_STATUSES

        status = "completed" if is_terminal else "running"
        if not is_terminal and any(
            e.get("type") == "workflow.paused" for e in events[-3:]
        ):
            status = "paused"

        return WorkflowStatus(
            workflow_id=workflow_id,
            status=status,
            steps=steps,
            events=events,
            is_terminal=is_terminal,
        )

    async def get_authz_context(self, workflow_id: str) -> dict[str, Any]:
        """Query authorization context from Temporal workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            result = await handle.query(AgentWorkflow.get_authz_context)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise
        return _to_dict(result)

    async def get_workflow_context(self, workflow_id: str) -> dict[str, Any]:
        """Query workflow context from Temporal workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            result = await handle.query(AgentWorkflow.get_workflow_context)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise
        return _to_dict(result)

    async def get_step_transcripts(self, workflow_id: str) -> dict[str, Any]:
        """Query step transcripts from Temporal workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            result = await handle.query(AgentWorkflow.get_step_transcripts)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise
        return _to_dict(result)

    async def is_terminal(self, workflow_id: str) -> bool:
        """Check if a Temporal workflow has reached a terminal state."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise KeyError(f"Workflow '{workflow_id}' not found") from exc
            raise
        return desc.status in _TERMINAL_STATUSES
