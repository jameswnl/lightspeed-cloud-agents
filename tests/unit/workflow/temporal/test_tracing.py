"""Unit tests for OTel tracing in Temporal activities."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture


class TestActivityTracing:
    """Tests that activities create and close OTel spans."""

    @pytest.mark.asyncio
    async def test_sandbox_step_creates_and_closes_span(
        self, mocker: MockerFixture,
    ) -> None:
        """run_sandbox_step creates a span and closes it."""
        mock_tracer = mocker.MagicMock()
        mock_cm = mocker.MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_cm
        mocker.patch(
            "agents.workflow.temporal_activities._tracer", mock_tracer,
        )

        from agents.workflow.temporal_activities import run_sandbox_step
        await run_sandbox_step({
            "step": {"name": "diag", "prompt": "check", "output_key": "r1"},
            "workflow_id": "wf-1",
            "provider": {"name": "openai", "model": "gpt-4", "credentials_secret": "k"},
            "sandbox_image": "sandbox:latest",
            "context": {},
        })

        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "sandbox.step"
        mock_cm.__enter__.assert_called_once()
        mock_cm.__exit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_creates_and_closes_span(
        self, mocker: MockerFixture,
    ) -> None:
        """send_approval_notification creates and closes a span."""
        mock_tracer = mocker.MagicMock()
        mock_cm = mocker.MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_cm
        mocker.patch(
            "agents.workflow.temporal_activities._tracer", mock_tracer,
        )
        mocker.patch(
            "agents.workflow.temporal_activities.NullNotifier",
        ).return_value = mocker.AsyncMock()

        from agents.workflow.temporal_activities import send_approval_notification
        await send_approval_notification({
            "workflow_id": "wf-1",
            "step_name": "approve",
            "message": "OK?",
            "notifier_config": None,
        })

        mock_tracer.start_as_current_span.assert_called_once()
        assert call_args[0][0] == "notification.send" if (call_args := mock_tracer.start_as_current_span.call_args) else False
        mock_cm.__enter__.assert_called_once()
        mock_cm.__exit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_escalation_creates_and_closes_span(
        self, mocker: MockerFixture,
    ) -> None:
        """build_escalation_activity creates and closes a span."""
        mock_tracer = mocker.MagicMock()
        mock_cm = mocker.MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_cm
        mocker.patch(
            "agents.workflow.temporal_activities._tracer", mock_tracer,
        )

        from agents.workflow.temporal_activities import build_escalation_activity
        await build_escalation_activity(
            {"r1": {"status": "failed", "error": "timeout"}},
        )

        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "escalation.build"
        mock_cm.__enter__.assert_called_once()
        mock_cm.__exit__.assert_called_once()
