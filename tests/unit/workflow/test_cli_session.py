"""Unit tests for CLI session launcher (T15 Phase 2, Task 1)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cloud_agents.workflow.cli_session import (
    CLISessionLauncher,
    CLISessionInfo,
    CLISessionStatus,
)


class TestCLISessionInfo:
    """Tests for CLISessionInfo model."""

    def test_session_info_creation(self) -> None:
        """CLISessionInfo captures all required fields."""
        info = CLISessionInfo(
            session_id="sess-abc123",
            agent_name="cli-agent-abc123",
            workflow_id="wf-123",
            started_at="2026-01-01T00:00:00+00:00",
            status=CLISessionStatus.RUNNING,
        )
        assert info.session_id == "sess-abc123"
        assert info.agent_name == "cli-agent-abc123"
        assert info.workflow_id == "wf-123"
        assert info.status == CLISessionStatus.RUNNING

    def test_session_info_defaults(self) -> None:
        """CLISessionInfo has sensible defaults for optional fields."""
        info = CLISessionInfo(
            session_id="sess-1",
            agent_name="cli-agent-1",
            workflow_id="wf-1",
            started_at="2026-01-01T00:00:00+00:00",
            status=CLISessionStatus.RUNNING,
        )
        assert info.endpoint is None
        assert info.error is None

    def test_session_status_enum_values(self) -> None:
        """CLISessionStatus has correct enum values."""
        assert CLISessionStatus.RUNNING == "running"
        assert CLISessionStatus.COMPLETED == "completed"
        assert CLISessionStatus.FAILED == "failed"
        assert CLISessionStatus.TERMINATED == "terminated"


class TestCLISessionLauncherInit:
    """Tests for CLISessionLauncher initialization."""

    def test_default_timeout(self) -> None:
        """Default session timeout is 3600 seconds (1 hour)."""
        launcher = CLISessionLauncher()
        assert launcher.max_session_seconds == 3600

    def test_custom_timeout(self) -> None:
        """Custom session timeout is respected."""
        launcher = CLISessionLauncher(max_session_seconds=1800)
        assert launcher.max_session_seconds == 1800

    def test_starts_with_no_sessions(self) -> None:
        """Launcher starts with no active sessions."""
        launcher = CLISessionLauncher()
        assert launcher.list_sessions() == []


class TestCLISessionLauncherLaunch:
    """Tests for CLISessionLauncher.launch()."""

    @pytest.mark.asyncio
    async def test_launch_calls_spawner_spawn(self) -> None:
        """launch() calls spawner.spawn() with correct arguments."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Handoff context",
            prompt="Investigate the failure",
            image="quay.io/sandbox:latest",
            workflow_id="wf-abc",
            env={"ANTHROPIC_API_KEY": "sk-test"},
        )

        assert session_id is not None
        spawner.spawn.assert_called_once()
        call_kwargs = spawner.spawn.call_args
        # The agent_name should start with "cli-"
        agent_name = call_kwargs[1]["agent_name"] if "agent_name" in call_kwargs[1] else call_kwargs[0][0]
        assert agent_name.startswith("cli-")

    @pytest.mark.asyncio
    async def test_launch_returns_session_id(self) -> None:
        """launch() returns a unique session ID."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-1",
        )

        assert session_id.startswith("cli-sess-")

    @pytest.mark.asyncio
    async def test_launch_tracks_session(self) -> None:
        """launch() adds the session to the internal tracker."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-track",
        )

        sessions = launcher.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == session_id
        assert sessions[0].workflow_id == "wf-track"
        assert sessions[0].status == CLISessionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_launch_passes_env_to_spawner(self) -> None:
        """launch() passes environment variables to the spawner."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-env",
            env={"ANTHROPIC_API_KEY": "sk-test", "CUSTOM_VAR": "value"},
        )

        call_kwargs = spawner.spawn.call_args[1]
        env = call_kwargs["env"]
        assert env["ANTHROPIC_API_KEY"] == "sk-test"
        assert env["CUSTOM_VAR"] == "value"

    @pytest.mark.asyncio
    async def test_launch_injects_context_and_prompt_env(self) -> None:
        """launch() injects CLI_HANDOFF_CONTEXT and CLI_HANDOFF_PROMPT into env."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# My handoff context",
            prompt="Continue investigation",
            image="quay.io/sandbox:latest",
            workflow_id="wf-ctx",
        )

        call_kwargs = spawner.spawn.call_args[1]
        env = call_kwargs["env"]
        assert env["CLI_HANDOFF_CONTEXT"] == "# My handoff context"
        assert env["CLI_HANDOFF_PROMPT"] == "Continue investigation"

    @pytest.mark.asyncio
    async def test_launch_emits_audit_event(self) -> None:
        """launch() emits a cli_session_launched audit event."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        with patch("cloud_agents.workflow.cli_session.emit_audit") as mock_audit:
            session_id = await launcher.launch(
                spawner=spawner,
                context_markdown="# Context",
                prompt="Investigate",
                image="quay.io/sandbox:latest",
                workflow_id="wf-audit",
            )

            launched_calls = [
                c for c in mock_audit.call_args_list
                if c[1].get("event_type") == "cli_session_launched"
            ]
            assert len(launched_calls) == 1
            assert launched_calls[0][1]["workflow_id"] == "wf-audit"
            assert launched_calls[0][1]["details"]["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_launch_failure_emits_failed_audit(self) -> None:
        """launch() emits cli_session_failed when spawner fails."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(side_effect=RuntimeError("spawner error"))

        launcher = CLISessionLauncher()
        with patch("cloud_agents.workflow.cli_session.emit_audit") as mock_audit:
            with pytest.raises(RuntimeError, match="spawner error"):
                await launcher.launch(
                    spawner=spawner,
                    context_markdown="# Context",
                    prompt="Investigate",
                    image="quay.io/sandbox:latest",
                    workflow_id="wf-fail",
                )

            failed_calls = [
                c for c in mock_audit.call_args_list
                if c[1].get("event_type") == "cli_session_failed"
            ]
            assert len(failed_calls) == 1
            assert "spawner error" in failed_calls[0][1]["details"]["error"]

    @pytest.mark.asyncio
    async def test_launch_uses_correct_image(self) -> None:
        """launch() passes the specified image to the spawner."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/custom-cli:v2",
            workflow_id="wf-img",
        )

        call_kwargs = spawner.spawn.call_args[1]
        assert call_kwargs["image"] == "quay.io/custom-cli:v2"

    @pytest.mark.asyncio
    async def test_launch_passes_labels(self) -> None:
        """launch() labels spawned container for tracking."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-label",
        )

        call_kwargs = spawner.spawn.call_args[1]
        labels = call_kwargs["labels"]
        assert labels["cloud-agents/session-type"] == "cli-handoff"
        assert labels["cloud-agents/workflow-id"] == "wf-label"


class TestCLISessionLauncherGetStatus:
    """Tests for CLISessionLauncher.get_status()."""

    @pytest.mark.asyncio
    async def test_get_status_returns_session_info(self) -> None:
        """get_status() returns session info for a tracked session."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-status",
        )

        info = launcher.get_status(session_id)
        assert info is not None
        assert info.session_id == session_id
        assert info.status == CLISessionStatus.RUNNING

    def test_get_status_returns_none_for_unknown(self) -> None:
        """get_status() returns None for unknown session IDs."""
        launcher = CLISessionLauncher()
        assert launcher.get_status("nonexistent") is None


class TestCLISessionLauncherTerminate:
    """Tests for CLISessionLauncher.terminate()."""

    @pytest.mark.asyncio
    async def test_terminate_calls_spawner_destroy(self) -> None:
        """terminate() calls spawner.destroy() with the agent name."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-term",
        )

        await launcher.terminate(session_id, spawner)
        spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminate_updates_status(self) -> None:
        """terminate() updates session status to terminated."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-term-status",
        )

        await launcher.terminate(session_id, spawner)
        info = launcher.get_status(session_id)
        assert info is not None
        assert info.status == CLISessionStatus.TERMINATED

    @pytest.mark.asyncio
    async def test_terminate_emits_audit_event(self) -> None:
        """terminate() emits a cli_session_terminated audit event."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-term-audit",
        )

        with patch("cloud_agents.workflow.cli_session.emit_audit") as mock_audit:
            await launcher.terminate(session_id, spawner)

            terminated_calls = [
                c for c in mock_audit.call_args_list
                if c[1].get("event_type") == "cli_session_terminated"
            ]
            assert len(terminated_calls) == 1
            assert terminated_calls[0][1]["details"]["session_id"] == session_id
            assert terminated_calls[0][1]["details"]["reason"] == "user_request"

    @pytest.mark.asyncio
    async def test_terminate_unknown_session_raises(self) -> None:
        """terminate() raises KeyError for unknown session IDs."""
        spawner = AsyncMock()
        launcher = CLISessionLauncher()

        with pytest.raises(KeyError):
            await launcher.terminate("nonexistent", spawner)

    @pytest.mark.asyncio
    async def test_terminate_destroy_failure_still_marks_failed(self) -> None:
        """terminate() marks session as failed when destroy raises."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock(side_effect=RuntimeError("destroy failed"))

        launcher = CLISessionLauncher()
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-term-fail",
        )

        with pytest.raises(RuntimeError, match="destroy failed"):
            await launcher.terminate(session_id, spawner)

        info = launcher.get_status(session_id)
        assert info is not None
        assert info.status == CLISessionStatus.FAILED


class TestCLISessionLauncherListSessions:
    """Tests for CLISessionLauncher.list_sessions()."""

    @pytest.mark.asyncio
    async def test_list_sessions_returns_all(self) -> None:
        """list_sessions() returns all tracked sessions."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context 1",
            prompt="Investigate 1",
            image="quay.io/sandbox:latest",
            workflow_id="wf-list-1",
        )
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context 2",
            prompt="Investigate 2",
            image="quay.io/sandbox:latest",
            workflow_id="wf-list-2",
        )

        sessions = launcher.list_sessions()
        assert len(sessions) == 2
        workflow_ids = {s.workflow_id for s in sessions}
        assert workflow_ids == {"wf-list-1", "wf-list-2"}

    @pytest.mark.asyncio
    async def test_list_sessions_filter_by_workflow_id(self) -> None:
        """list_sessions() can filter by workflow_id."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")

        launcher = CLISessionLauncher()
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-filter-a",
        )
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-filter-b",
        )

        sessions = launcher.list_sessions(workflow_id="wf-filter-a")
        assert len(sessions) == 1
        assert sessions[0].workflow_id == "wf-filter-a"


class TestCLISessionTimeoutEnforcement:
    """Tests for session timeout enforcement background task."""

    @pytest.mark.asyncio
    async def test_start_timeout_monitor_creates_background_task(self) -> None:
        """start_timeout_monitor() creates a background asyncio task."""
        spawner = AsyncMock()
        launcher = CLISessionLauncher(max_session_seconds=60)

        launcher.start_timeout_monitor(spawner)
        try:
            assert launcher._timeout_task is not None
            assert not launcher._timeout_task.done()
        finally:
            await launcher.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_monitor_terminates_expired_session(self) -> None:
        """Timeout monitor auto-terminates sessions exceeding max_session_seconds."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        # Use a very short timeout so the session expires immediately
        launcher = CLISessionLauncher(max_session_seconds=0)
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-timeout",
        )

        # Patch the check interval to be very short for testing
        with patch.object(launcher, "_check_interval", 0.01):
            launcher.start_timeout_monitor(spawner)
            # Give the monitor time to run one cycle
            await asyncio.sleep(0.1)
            await launcher.shutdown()

        info = launcher.get_status(session_id)
        assert info is not None
        assert info.status == CLISessionStatus.TERMINATED
        spawner.destroy.assert_called_once_with(info.agent_name)

    @pytest.mark.asyncio
    async def test_timeout_monitor_emits_audit_event_with_timeout_reason(self) -> None:
        """Timeout termination emits cli_session_terminated with reason=timeout."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        launcher = CLISessionLauncher(max_session_seconds=0)
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-audit-timeout",
        )

        with (
            patch("cloud_agents.workflow.cli_session.emit_audit") as mock_audit,
            patch.object(launcher, "_check_interval", 0.01),
        ):
            launcher.start_timeout_monitor(spawner)
            await asyncio.sleep(0.1)
            await launcher.shutdown()

            terminated_calls = [
                c
                for c in mock_audit.call_args_list
                if c[1].get("event_type") == "cli_session_terminated"
            ]
            assert len(terminated_calls) == 1
            details = terminated_calls[0][1]["details"]
            assert details["session_id"] == session_id
            assert details["reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_timeout_monitor_skips_non_running_sessions(self) -> None:
        """Timeout monitor only terminates RUNNING sessions."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        launcher = CLISessionLauncher(max_session_seconds=0)
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-skip",
        )

        # Manually mark session as already terminated
        launcher._sessions[session_id].status = CLISessionStatus.TERMINATED

        with patch.object(launcher, "_check_interval", 0.01):
            launcher.start_timeout_monitor(spawner)
            await asyncio.sleep(0.1)
            await launcher.shutdown()

        # destroy should not have been called (session was already terminated)
        spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_monitor_handles_destroy_failure_gracefully(self) -> None:
        """Timeout monitor marks session FAILED if destroy raises."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock(side_effect=RuntimeError("pod gone"))

        launcher = CLISessionLauncher(max_session_seconds=0)
        session_id = await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-fail-timeout",
        )

        with patch.object(launcher, "_check_interval", 0.01):
            launcher.start_timeout_monitor(spawner)
            await asyncio.sleep(0.1)
            await launcher.shutdown()

        info = launcher.get_status(session_id)
        assert info is not None
        assert info.status == CLISessionStatus.FAILED
        assert info.error is not None
        assert "pod gone" in info.error

    @pytest.mark.asyncio
    async def test_shutdown_cancels_timeout_task(self) -> None:
        """shutdown() cancels the background timeout monitor task."""
        spawner = AsyncMock()
        launcher = CLISessionLauncher(max_session_seconds=3600)

        launcher.start_timeout_monitor(spawner)
        task = launcher._timeout_task
        assert task is not None
        assert not task.done()

        await launcher.shutdown()
        assert task.cancelled() or task.done()
        assert launcher._timeout_task is None

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self) -> None:
        """shutdown() is safe to call multiple times."""
        launcher = CLISessionLauncher()
        # Should not raise even without a monitor running
        await launcher.shutdown()
        await launcher.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_monitor_does_not_terminate_fresh_sessions(self) -> None:
        """Sessions within max_session_seconds are not terminated."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://cli-agent:8080")
        spawner.destroy = AsyncMock()

        # Long timeout, session should not be terminated
        launcher = CLISessionLauncher(max_session_seconds=3600)
        await launcher.launch(
            spawner=spawner,
            context_markdown="# Context",
            prompt="Investigate",
            image="quay.io/sandbox:latest",
            workflow_id="wf-fresh",
        )

        with patch.object(launcher, "_check_interval", 0.01):
            launcher.start_timeout_monitor(spawner)
            await asyncio.sleep(0.1)
            await launcher.shutdown()

        sessions = launcher.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == CLISessionStatus.RUNNING
        spawner.destroy.assert_not_called()
