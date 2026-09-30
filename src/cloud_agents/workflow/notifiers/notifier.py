"""Approval notification for workflow steps.

Fire-and-forget notification when a workflow pauses for human approval.
Actual approval still comes via POST /v1/workflows/{id}/approve.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)


class ApprovalNotifier(Protocol):
    """Protocol for approval notification implementations."""

    async def notify(
        self,
        workflow_id: str,
        step_name: str,
        message: str,
        approve_url: str,
    ) -> None:
        """Send an approval notification."""
        ...


class NullNotifier:
    """No-op notifier — does nothing."""

    async def notify(
        self,
        workflow_id: str,
        step_name: str,
        message: str,
        approve_url: str,
    ) -> None:
        """No-op."""


class SlackNotifier:
    """Sends approval notifications to a Slack webhook.

    Attributes:
        webhook_url: Slack incoming webhook URL.
        channel: Optional channel override.
    """

    def __init__(self, webhook_url: str, channel: Optional[str] = None) -> None:
        """Initialize the Slack notifier.

        Args:
            webhook_url: Slack incoming webhook URL.
            channel: Optional channel override.
        """
        self.webhook_url = webhook_url
        self.channel = channel

    async def notify(
        self,
        workflow_id: str,
        step_name: str,
        message: str,
        approve_url: str,
    ) -> None:
        """Send a Slack notification with approval details."""
        payload: dict[str, Any] = {
            "text": f"Workflow approval required: {message}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Workflow Approval Required*\n"
                            f"Step: `{step_name}`\n"
                            f"Message: {message}\n"
                            f"Workflow ID: `{workflow_id}`\n"
                            f"Approve: `POST {approve_url}`"
                        ),
                    },
                },
            ],
        }
        if self.channel:
            payload["channel"] = self.channel

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("Slack notification sent for step '%s'", step_name)
        except Exception as exc:
            logger.warning("Slack notification failed for step '%s': %s", step_name, exc)


class WebhookNotifier:
    """Sends approval notifications to a generic webhook.

    Attributes:
        url: Webhook endpoint URL.
    """

    def __init__(self, url: str) -> None:
        """Initialize the webhook notifier.

        Args:
            url: Webhook endpoint URL.
        """
        self.url = url

    async def notify(
        self,
        workflow_id: str,
        step_name: str,
        message: str,
        approve_url: str,
    ) -> None:
        """Send a webhook notification with approval details."""
        payload = {
            "event": "approval_required",
            "workflow_id": workflow_id,
            "step_name": step_name,
            "message": message,
            "approve_url": approve_url,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.url, json=payload)
                resp.raise_for_status()
            logger.info("Webhook notification sent for step '%s'", step_name)
        except Exception as exc:
            logger.warning("Webhook notification failed for step '%s': %s", step_name, exc)
