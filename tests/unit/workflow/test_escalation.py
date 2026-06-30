"""Unit tests for escalation packaging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_agents.workflow.escalation import (
    EscalationPackage,
    JiraPackager,
    LogPackager,
    WebhookPackager,
    build_escalation_package,
)


def _make_package() -> EscalationPackage:
    """Create a test escalation package."""
    return build_escalation_package(
        workflow_name="diagnose-fix",
        step_name="fix-hosts",
        escalation_data={
            "failure_history": [{"error": "timeout"}],
            "recommendation": "manual fix",
        },
        workflow_snapshot={"diagnosis": {"summary": "3 hosts down"}},
        correlation_id="corr-123",
    )


class TestBuildEscalationPackage:
    """Tests for build_escalation_package."""

    def test_creates_package(self) -> None:
        """Test that a package is created with all fields."""
        pkg = _make_package()
        assert pkg.workflow_name == "diagnose-fix"
        assert pkg.step_name == "fix-hosts"
        assert pkg.correlation_id == "corr-123"
        assert pkg.timestamp is not None
        assert pkg.escalation["recommendation"] == "manual fix"
        assert pkg.workflow_snapshot["diagnosis"]["summary"] == "3 hosts down"


class TestLogPackager:
    """Tests for LogPackager."""

    @pytest.mark.asyncio
    async def test_logs_without_error(self) -> None:
        """Test that LogPackager logs the escalation."""
        packager = LogPackager()
        await packager.package(_make_package())


class TestWebhookPackager:
    """Tests for WebhookPackager."""

    @pytest.mark.asyncio
    async def test_sends_payload(self) -> None:
        """Test that webhook sends correct payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = WebhookPackager("http://hooks.example.com/escalation")
            await packager.package(_make_package())

            payload = mock_client.post.call_args[1]["json"]
            assert payload["workflow_name"] == "diagnose-fix"
            assert payload["step_name"] == "fix-hosts"

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self) -> None:
        """Test that webhook failures are logged, not raised."""
        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = WebhookPackager("http://hooks.example.com/escalation")
            await packager.package(_make_package())


class TestJiraPackager:
    """Tests for JiraPackager."""

    @pytest.mark.asyncio
    async def test_creates_issue(self) -> None:
        """Test that Jira packager sends correct issue payload."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("cloud_agents.workflow.escalation.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            packager = JiraPackager("https://jira.example.com", "OPS")
            await packager.package(_make_package())

            call_args = mock_client.post.call_args
            assert "/rest/api/2/issue" in call_args[0][0]
            payload = call_args[1]["json"]
            assert payload["fields"]["project"]["key"] == "OPS"
            assert "fix-hosts" in payload["fields"]["summary"]
