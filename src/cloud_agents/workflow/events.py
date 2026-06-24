"""Workflow event model for SSE streaming.

Defines event types emitted at workflow state transitions for
real-time progress monitoring via Server-Sent Events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "workflow.started",
    "step.started",
    "step.completed",
    "step.failed",
    "step.skipped",
    "workflow.paused",
    "workflow.completed",
    "workflow.failed",
]


class WorkflowEvent(BaseModel):
    """A workflow state transition event.

    Attributes:
        event_type: The type of event.
        workflow_id: The workflow instance ID.
        timestamp: ISO timestamp of the event.
        step_name: Step name (for step-level events).
        data: Additional event data.
    """

    event_type: EventType
    workflow_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    step_name: Optional[str] = None
    data: Optional[dict[str, Any]] = None

    def to_sse(self) -> str:
        """Format as a Server-Sent Event string."""
        payload = self.model_dump(mode="json", exclude_none=True)
        return f"event: {self.event_type}\ndata: {json.dumps(payload)}\n\n"
