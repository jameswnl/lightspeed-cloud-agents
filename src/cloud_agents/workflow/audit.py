"""Structured audit events for security-relevant workflow actions.

Emits JSON-serializable audit events for compliance and forensics:
workflow lifecycle, approval decisions, sandbox spawning, escalation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

AuditEventType = Literal[
    "workflow_started",
    "step_approved",
    "step_denied",
    "sandbox_spawned",
    "sandbox_destroyed",
    "sandbox_timeout",
    "escalation_triggered",
    "mcp_secret_mounted",
    "orphan_cleanup",
    "content_policy_violation",
    "auth_rejected",
    "rate_limit_exceeded",
    "alert_triggered",
    "alert_validation_failed",
    "schedule_created",
    "schedule_deleted",
    "schedule_triggered",
]


class AuditEvent(BaseModel):
    """A security-relevant workflow event for audit trail.

    Attributes:
        event_type: Action identifier.
        workflow_id: Workflow run ID.
        step_name: Step within the workflow, if applicable.
        actor: Identity that triggered the action.
        risk_level: Risk classification of the action.
        details: Additional context for the event.
        timestamp: ISO 8601 timestamp.
    """

    event_type: AuditEventType
    workflow_id: str
    step_name: Optional[str] = None
    actor: Optional[str] = None
    risk_level: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


def emit_audit(
    event_type: AuditEventType,
    workflow_id: str = "",
    step_name: str | None = None,
    actor: str | None = None,
    risk_level: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Create and log an audit event.

    Parameters:
        event_type: Action identifier.
        workflow_id: Workflow run ID. Defaults to empty string for
            pre-workflow events (e.g. auth rejection).
        step_name: Step name, if applicable.
        actor: Identity that triggered the action.
        risk_level: Risk classification.
        details: Additional context.

    Returns:
        The emitted AuditEvent.
    """
    event = AuditEvent(
        event_type=event_type,
        workflow_id=workflow_id,
        step_name=step_name,
        actor=actor,
        risk_level=risk_level,
        details=details or {},
    )
    logger.info("audit: %s %s", event.event_type, event.model_dump_json())
    return event
