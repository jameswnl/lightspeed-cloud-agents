"""Unit tests for executor dual-mode dispatch (Phase 8 Task 6)."""

import pytest
from unittest.mock import AsyncMock, patch

from agents.models import AgentRunResponse
from agents.registry import AgentRegistry
from agents.workflow.definition import WorkflowDefinition, WorkflowSpec, WorkflowStepSpec
from agents.workflow.executor import WorkflowExecutor
from agents.workflow.persistence import InMemoryPersistence
from agents.workflow.state import StepResult


def _make_definition(spawn: str = "ephemeral") -> WorkflowDefinition:
    return WorkflowDefinition(
        apiVersion="v1", kind="AgentWorkflow",
        metadata={"name": "test-wf"},
        spec=WorkflowSpec(steps=[
            WorkflowStepSpec(
                name="step1", type="agent", agent="diag",
                prompt="test", output_key="result1", spawn=spawn,
            ),
        ]),
    )


def _make_response(output: dict) -> AgentRunResponse:
    return AgentRunResponse(
        output=output, output_type="str",
        usage={"input_tokens": 1, "output_tokens": 1},
        agent_name="diag", success=True,
    )


class TestExecutorDualMode:
    """Tests for sync vs async dispatch mode selection."""

    @pytest.mark.asyncio
    async def test_no_callback_url_uses_sync(self) -> None:
        """Without callback_base_url, executor uses sync dispatch."""
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"ok": True}))
        registry = AgentRegistry({"diag": "http://diag:8080"})

        executor = WorkflowExecutor(
            _make_definition(spawn="pre-deployed"),
            registry,
            client_factory=lambda _: client,
        )
        state = await executor.run()

        assert state.status == "completed"
        client.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_url_with_ephemeral_uses_async(self) -> None:
        """With callback_base_url, ephemeral steps use async dispatch."""
        spawner = AsyncMock()
        spawner.spawn = AsyncMock(return_value="http://spawned:8080")
        spawner.wait_ready = AsyncMock(return_value=True)

        client = AsyncMock()
        client.run_async = AsyncMock(return_value="run-123")

        registry = AgentRegistry({"diag": "http://diag:8080"})

        with patch("agents.workflow.step_dispatcher.RemoteAgentClient", return_value=client), \
             patch("agents.runtime.auth.get_api_token", return_value=None):
            executor = WorkflowExecutor(
                _make_definition(spawn="ephemeral"),
                registry,
                client_factory=lambda _: client,
                spawner=spawner,
                callback_base_url="http://runner:8080",
            )
            state = await executor.run()

        assert state.status == "running"
        assert "result1" in state.steps
        assert state.steps["result1"].status == "dispatched"

    @pytest.mark.asyncio
    async def test_pre_deployed_stays_sync_even_with_callback(self) -> None:
        """Pre-deployed agents always use sync dispatch."""
        client = AsyncMock()
        client.run = AsyncMock(return_value=_make_response({"ok": True}))
        registry = AgentRegistry({"diag": "http://diag:8080"})

        executor = WorkflowExecutor(
            _make_definition(spawn="pre-deployed"),
            registry,
            client_factory=lambda _: client,
            callback_base_url="http://runner:8080",
        )
        state = await executor.run()

        assert state.status == "completed"
        client.run.assert_called_once()
