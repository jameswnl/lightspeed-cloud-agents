"""Workflow runner interface.

Defines the contract between the API layer and workflow execution backends.
Implementations: TemporalWorkflowRunner (Tier 2), LocalWorkflowRunner (Tier 1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WorkflowStatus:
    """Snapshot of a workflow execution's current state.

    Attributes:
        workflow_id: Unique execution identifier.
        status: Overall status (running, paused, completed, failed, cancelled).
        steps: Per-step results keyed by output_key.
        events: Ordered list of workflow lifecycle events.
        is_terminal: Whether the workflow has reached a final state.
    """

    workflow_id: str
    status: str
    steps: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    is_terminal: bool = False


@dataclass
class ApprovalDecision:
    """Human approval decision for a workflow step.

    Attributes:
        step_name: Name of the approval step.
        decision: Approval decision string (e.g. "approved", "rejected").
        approver: Identity of the approver.
        selected_option_id: Optional selected remediation option.
    """

    step_name: str
    decision: str
    approver: str = ""
    selected_option_id: Optional[str] = None


class WorkflowRunner(ABC):
    """Abstract interface for workflow execution backends.

    The API layer dispatches to this interface. Implementations handle
    the details of workflow orchestration, state persistence, and
    signal delivery.
    """

    @abstractmethod
    async def start(self, input: dict[str, Any]) -> str:
        """Start a new workflow execution.

        Parameters:
            input: Workflow input including definition, provider config,
                sandbox image, skills, MCP servers, approval policy.

        Returns:
            Workflow execution ID.

        Raises:
            ValueError: If a workflow with the same ID already exists.
        """

    @abstractmethod
    async def approve(
        self,
        workflow_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Send an approval signal to a paused workflow.

        Parameters:
            workflow_id: Target workflow execution.
            decision: Approval decision with step name, verdict, and approver.

        Raises:
            KeyError: If the workflow does not exist.
            RuntimeError: If the workflow is not paused at the specified step.
        """

    @abstractmethod
    async def cancel(self, workflow_id: str) -> None:
        """Cancel a running workflow.

        Parameters:
            workflow_id: Target workflow execution.

        Raises:
            KeyError: If the workflow does not exist.
        """

    @abstractmethod
    async def get_status(self, workflow_id: str) -> WorkflowStatus:
        """Get the current status of a workflow execution.

        Parameters:
            workflow_id: Target workflow execution.

        Returns:
            Current workflow status including step results and events.

        Raises:
            KeyError: If the workflow does not exist.
        """

    @abstractmethod
    async def get_authz_context(self, workflow_id: str) -> dict[str, Any]:
        """Get the authorization context for a workflow.

        Parameters:
            workflow_id: Target workflow execution.

        Returns:
            Authorization context dict (caller identity, workflow name, etc.).

        Raises:
            KeyError: If the workflow does not exist.
        """

    @abstractmethod
    async def get_workflow_context(self, workflow_id: str) -> dict[str, Any]:
        """Get the full workflow context for escalation handoff.

        Parameters:
            workflow_id: Target workflow execution.

        Returns:
            Workflow context including definition, step results, transcripts.

        Raises:
            KeyError: If the workflow does not exist.
        """

    @abstractmethod
    async def get_step_transcripts(self, workflow_id: str) -> dict[str, Any]:
        """Get agent execution transcripts for all completed steps.

        Parameters:
            workflow_id: Target workflow execution.

        Returns:
            Transcripts keyed by step output_key.

        Raises:
            KeyError: If the workflow does not exist.
        """

    @abstractmethod
    async def is_terminal(self, workflow_id: str) -> bool:
        """Check whether a workflow has reached a terminal state.

        Parameters:
            workflow_id: Target workflow execution.

        Returns:
            True if the workflow is completed, failed, or cancelled.

        Raises:
            KeyError: If the workflow does not exist.
        """

    async def send_message(self, workflow_id: str, prompt: str) -> Any:
        """Append and execute a step interactively.

        Override for interactive runners (ChatWorkflowRunner).
        Predefined workflow runners raise NotImplementedError.

        Parameters:
            workflow_id: Target workflow/conversation ID.
            prompt: User message.

        Returns:
            Step execution result.

        Raises:
            NotImplementedError: If this runner does not support interactive messages.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive messages"
        )

    async def send_message_stream(
        self, workflow_id: str, prompt: str
    ) -> AsyncIterator:
        """Append and execute a step interactively with streaming.

        Override for interactive runners (ChatWorkflowRunner).

        Parameters:
            workflow_id: Target workflow/conversation ID.
            prompt: User message.

        Yields:
            Streaming events.

        Raises:
            NotImplementedError: If this runner does not support interactive streaming.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive streaming"
        )
        yield  # make it a generator
