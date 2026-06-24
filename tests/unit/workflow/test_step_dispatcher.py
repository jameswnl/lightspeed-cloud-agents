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
