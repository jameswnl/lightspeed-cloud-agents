"""Escalation packaging for workflow failures.

Packages escalation handoff documents and sends them to external
systems (Jira, webhook, or structured log).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Optional, Protocol

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EscalationPackage(BaseModel):
    """Complete escalation package for external systems.

    Attributes:
        workflow_name: Name of the failed workflow.
        step_name: Step that exhausted retries.
        correlation_id: Trace correlation ID.
        timestamp: When the escalation was generated.
        escalation: The raw escalation handoff data.
        workflow_snapshot: Current state of all workflow steps.
    """

    workflow_name: str
    step_name: str
    correlation_id: Optional[str] = None
    timestamp: str
    escalation: dict[str, Any]
    workflow_snapshot: dict[str, Any]


class EscalationPackager(Protocol):
    """Protocol for escalation delivery implementations."""

    async def package(self, pkg: EscalationPackage) -> None:
        """Deliver an escalation package."""
        ...


class LogPackager:
    """Logs escalation as structured JSON (default for PoC)."""

    async def package(self, pkg: EscalationPackage) -> None:
        """Log the escalation package."""
        logger.error(
            "ESCALATION: %s",
            json.dumps(pkg.model_dump(mode="json"), indent=2),
        )


class WebhookPackager:
    """Sends escalation to a generic webhook.

    Attributes:
        url: Webhook endpoint URL.
    """

    def __init__(self, url: str) -> None:
        """Initialize the webhook packager.

        Args:
            url: Webhook endpoint URL.
        """
        self.url = url

    async def package(self, pkg: EscalationPackage) -> None:
        """POST the escalation package to the webhook."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.url,
                    json=pkg.model_dump(mode="json"),
                )
                resp.raise_for_status()
            logger.info("Escalation sent to webhook for step '%s'", pkg.step_name)
        except Exception as exc:
            logger.warning(
                "Escalation webhook failed for step '%s': %s", pkg.step_name, exc
            )


class JiraPackager:
    """Creates a Jira issue with the escalation details.

    Attributes:
        url: Jira REST API base URL.
        project_key: Jira project key.
    """

    def __init__(self, url: str, project_key: str) -> None:
        """Initialize the Jira packager.

        Args:
            url: Jira REST API base URL.
            project_key: Jira project key for issue creation.
        """
        self.url = url
        self.project_key = project_key

    async def package(self, pkg: EscalationPackage) -> None:
        """Create a Jira issue with the escalation details."""
        issue_data = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": f"Escalation: {pkg.workflow_name} / {pkg.step_name}",
                "description": json.dumps(pkg.model_dump(mode="json"), indent=2),
                "issuetype": {"name": "Bug"},
            }
        }
        auth_token = __import__("os").environ.get("JIRA_API_TOKEN", "")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.url}/rest/api/2/issue",
                    json=issue_data,
                    headers=headers,
                )
                resp.raise_for_status()
            logger.info(
                "Jira issue created for escalation: %s/%s",
                pkg.workflow_name,
                pkg.step_name,
            )
        except Exception as exc:
            logger.warning(
                "Jira escalation failed for step '%s': %s", pkg.step_name, exc
            )


def build_escalation_package(
    workflow_name: str,
    step_name: str,
    escalation_data: dict[str, Any],
    workflow_snapshot: dict[str, Any],
    correlation_id: Optional[str] = None,
) -> EscalationPackage:
    """Build an escalation package from workflow state.

    Args:
        workflow_name: Name of the failed workflow.
        step_name: Step that exhausted retries.
        escalation_data: Raw escalation handoff dict.
        workflow_snapshot: Current workflow state snapshot.
        correlation_id: Optional trace correlation ID.

    Returns:
        Complete EscalationPackage ready for delivery.
    """
    return EscalationPackage(
        workflow_name=workflow_name,
        step_name=step_name,
        correlation_id=correlation_id,
        timestamp=datetime.now(UTC).isoformat(),
        escalation=escalation_data,
        workflow_snapshot=workflow_snapshot,
    )
