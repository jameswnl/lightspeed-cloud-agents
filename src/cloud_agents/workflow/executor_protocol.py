"""Protocol defining the workflow executor interface.

Both WorkflowExecutor and GraphExecutor implement this protocol,
enabling side-by-side comparison and runtime selection.
"""

from __future__ import annotations

from typing import Optional, Protocol

from agents.workflow.state import WorkflowState


class WorkflowExecutorProtocol(Protocol):
    """Runtime interface for workflow executors."""

    async def run(self, input_prompt: Optional[str] = None) -> WorkflowState:
        """Execute the workflow from start."""
        ...

    async def resume(self, workflow_id: str, approved: bool = True) -> WorkflowState:
        """Resume a paused workflow after human approval."""
        ...

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        """Get current workflow state."""
        ...

    async def list_workflows(self) -> list[WorkflowState]:
        """List all tracked workflows."""
        ...
