"""Unit tests for approval notifiers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_agents.workflow.notifier import NullNotifier, SlackNotifier, WebhookNotifier


class TestNullNotifier:
    """Tests for the no-op notifier."""

    @pytest.mark.asyncio
    async def test_does_nothing(self) -> None:
        """Test that NullNotifier.notify() completes without error."""
        notifier = NullNotifier()
        await notifier.notify("wf-1", "approve", "OK?", "http://host/approve")


class TestSlackNotifier:
    """Tests for Slack webhook notifier."""

    @pytest.mark.asyncio
    async def test_sends_payload(self) -> None:
        """Test that Slack notification sends correct payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notifier = SlackNotifier("https://hooks.slack.com/test")
            await notifier.notify(
                "wf-1", "approve-fix", "Approve?", "http://wf/approve"
            )

            mock_client.post.assert_called_once()
            payload = mock_client.post.call_args[1]["json"]
            assert "Workflow Approval Required" in payload["blocks"][0]["text"]["text"]
            assert "approve-fix" in payload["blocks"][0]["text"]["text"]

    @pytest.mark.asyncio
    async def test_includes_channel(self) -> None:
        """Test that channel override is included."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notifier = SlackNotifier("https://hooks.slack.com/test", channel="#ops")
            await notifier.notify("wf-1", "step", "msg", "http://url")

            payload = mock_client.post.call_args[1]["json"]
            assert payload["channel"] == "#ops"

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self) -> None:
        """Test that Slack failures are logged, not raised."""
        with patch("cloud_agents.workflow.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("network error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notifier = SlackNotifier("https://hooks.slack.com/test")
            await notifier.notify("wf-1", "step", "msg", "http://url")


class TestWebhookNotifier:
    """Tests for generic webhook notifier."""

    @pytest.mark.asyncio
    async def test_sends_payload(self) -> None:
        """Test that webhook sends correct event payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notifier = WebhookNotifier("http://hooks.example.com/approval")
            await notifier.notify(
                "wf-1", "approve-fix", "Approve?", "http://wf/approve"
            )

            payload = mock_client.post.call_args[1]["json"]
            assert payload["event"] == "approval_required"
            assert payload["workflow_id"] == "wf-1"
            assert payload["step_name"] == "approve-fix"
