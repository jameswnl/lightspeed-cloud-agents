"""Unit tests for StepDispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.models import AgentRunResponse
from agents.workflow.definition import WorkflowStepSpec
from agents.workflow.step_dispatcher import StepDispatcher


def _make_step(name: str = "s1", spawn: str = "pre-deployed") -> WorkflowStepSpec:
    return WorkflowStepSpec(
        name=name, type="agent", agent="diag",
        prompt="test", output_key="r", spawn=spawn,
    )


def _make_response(output: dict) -> AgentRunResponse:
    return AgentRunResponse(
        output=output, output_type="str",
        usage={"input_tokens": 1, "output_tokens": 1},
        agent_name="diag", success=True,
    )


class TestStepDispatcherPreDeployed:
    """Tests for pre-deployed agent dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_success(self) -> None:
        """Test successful dispatch to pre-deployed agent."""
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"ok": True}))

        dispatcher = StepDispatcher(client_factory=lambda _: client)
        result = await dispatcher.dispatch(
            _make_step(), "test prompt", "wf-1",
        )

        assert result.status == "completed"
        assert result.output["ok"] is True

    @pytest.mark.asyncio
    async def test_dispatch_failure(self) -> None:
        """Test dispatch failure handling."""
        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Connection refused"))

        dispatcher = StepDispatcher(client_factory=lambda _: client)
        result = await dispatcher.dispatch(
            _make_step(), "test", "wf-1",
        )

        assert result.status == "failed"
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_passes_context(self) -> None:
        """Test that context is forwarded to agent client."""
        received_ctx = {}

        async def capturing_run(prompt, context=None):
            received_ctx.update(context or {})
            return _make_response({"ok": True})

        client = AsyncMock()
        client.run = capturing_run

        dispatcher = StepDispatcher(client_factory=lambda _: client)
        await dispatcher.dispatch(
            _make_step(), "test", "wf-1",
            context={"correlation_id": "abc-123"},
        )

        assert received_ctx.get("correlation_id") == "abc-123"


class TestStepDispatcherAsync:
    """Tests for async dispatch (fire-and-forget)."""

    @pytest.mark.asyncio
    async def test_dispatch_async_returns_dispatched_status(self) -> None:
        """Async dispatch returns dispatched status without blocking."""
        from agents.workflow.persistence import InMemoryPersistence
        from agents.workflow.state import StepResult, WorkflowState

        persistence = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test", status="running",
            steps={},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await persistence.save(state)

        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)

        client = AsyncMock()
        client.run_async = AsyncMock(return_value="run-123")

        from unittest.mock import patch
        dispatcher = StepDispatcher(
            client_factory=lambda _: client,
            spawner=spawner,
            callback_base_url="http://runner:8080",
        )
        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client):
            result = await dispatcher.dispatch_async(
                _make_step(spawn="ephemeral"), "test prompt", "wf-1",
                persistence=persistence, attempt=1,
            )

        assert result.status == "dispatched"
        assert result.output["run_id"] == "run-123"
        assert result.output["attempt"] == 1
        assert result.output["endpoint"] == "http://spawned:8080"

    @pytest.mark.asyncio
    async def test_dispatch_async_does_not_destroy_pod(self) -> None:
        """Async dispatch does NOT destroy the pod."""
        from agents.workflow.persistence import InMemoryPersistence
        from agents.workflow.state import WorkflowState

        persistence = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test", status="running",
            steps={},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await persistence.save(state)

        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)
        spawner.destroy = AsyncMock()

        client = AsyncMock()
        client.run_async = AsyncMock(return_value="run-123")

        from unittest.mock import patch
        dispatcher = StepDispatcher(
            client_factory=lambda _: client,
            spawner=spawner,
            callback_base_url="http://runner:8080",
        )
        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client):
            await dispatcher.dispatch_async(
                _make_step(spawn="ephemeral"), "test prompt", "wf-1",
                persistence=persistence, attempt=1,
            )

        spawner.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_async_passes_callback_url_in_env(self) -> None:
        """Async dispatch passes RESULT_CALLBACK_URL to spawned pod."""
        from agents.workflow.persistence import InMemoryPersistence
        from agents.workflow.state import WorkflowState

        persistence = InMemoryPersistence()
        state = WorkflowState(
            workflow_id="wf-1", workflow_name="test", status="running",
            steps={},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        await persistence.save(state)

        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)

        client = AsyncMock()
        client.run_async = AsyncMock(return_value="run-123")

        from unittest.mock import patch
        dispatcher = StepDispatcher(
            client_factory=lambda _: client,
            spawner=spawner,
            callback_base_url="http://runner:8080",
        )
        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client):
            await dispatcher.dispatch_async(
                _make_step(name="diagnose", spawn="ephemeral"),
                "test", "wf-1",
                persistence=persistence, attempt=1,
            )

        spawn_call = spawner.spawn.call_args
        env = spawn_call[1].get("env", spawn_call[0][2] if len(spawn_call[0]) > 2 else {})
        assert "RESULT_CALLBACK_URL" in env
        assert "wf-1" in env["RESULT_CALLBACK_URL"]
        assert "/steps/" in env["RESULT_CALLBACK_URL"]


class TestStepDispatcherEphemeral:
    """Tests for ephemeral spawn dispatch."""

    @pytest.mark.asyncio
    async def test_ephemeral_spawns_and_destroys(self) -> None:
        """Test that ephemeral dispatch spawns and destroys."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)
        spawner.destroy = AsyncMock()

        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"ok": True}))

        from unittest.mock import patch
        dispatcher = StepDispatcher(
            client_factory=lambda _: client,
            spawner=spawner,
        )
        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client):
            result = await dispatcher.dispatch(
                _make_step(spawn="ephemeral"), "test", "wf-1",
            )

        assert result.status == "completed"
        spawner.spawn.assert_called_once()
        spawner.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_ephemeral_cleanup_on_failure(self) -> None:
        """Test that spawned pod is destroyed even on failure."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)
        spawner.destroy = AsyncMock()

        client = AsyncMock()
        client.run = AsyncMock(side_effect=Exception("Agent crashed"))

        from unittest.mock import patch
        dispatcher = StepDispatcher(
            client_factory=lambda _: client,
            spawner=spawner,
        )
        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client):
            result = await dispatcher.dispatch(
                _make_step(spawn="ephemeral"), "test", "wf-1",
            )

        assert result.status == "failed"
        spawner.destroy.assert_called_once()
