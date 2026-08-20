"""Tests for step executor dispatch based on spawn mode."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture


class TestGetStepExecutor:
    """Tests for the get_step_executor factory."""

    def test_ephemeral_returns_sandbox_executor(self) -> None:
        """spawn: ephemeral returns SandboxExecutor."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = get_step_executor(
            step={"name": "s1", "spawn": "ephemeral"},
            spawner=object(),
        )
        assert isinstance(executor, SandboxExecutor)

    def test_default_is_ephemeral(self) -> None:
        """Missing spawn field defaults to ephemeral."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = get_step_executor(
            step={"name": "s1"},
            spawner=object(),
        )
        assert isinstance(executor, SandboxExecutor)

    def test_ephemeral_no_spawner_raises(self) -> None:
        """spawn: ephemeral without spawner raises ValueError."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        with pytest.raises(ValueError, match="spawner"):
            get_step_executor(
                step={"name": "s1", "spawn": "ephemeral"},
                spawner=None,
            )

    def test_none_raises_not_implemented(self) -> None:
        """spawn: none raises NotImplementedError (Phase 1 work)."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        with pytest.raises(NotImplementedError, match="none"):
            get_step_executor(
                step={"name": "s1", "spawn": "none"},
                spawner=None,
            )

    def test_local_raises_not_implemented(self) -> None:
        """spawn: local raises NotImplementedError (Phase 2 work)."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        with pytest.raises(NotImplementedError, match="local"):
            get_step_executor(
                step={"name": "s1", "spawn": "local"},
                spawner=None,
            )

    def test_unknown_spawn_raises(self) -> None:
        """Unknown spawn mode raises ValueError."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        with pytest.raises(ValueError, match="unknown_mode"):
            get_step_executor(
                step={"name": "s1", "spawn": "unknown_mode"},
                spawner=None,
            )


class TestGraphTranslatorUsesStepExecutor:
    """Tests that graph_translator dispatches via StepExecutor."""

    @pytest.mark.asyncio
    async def test_agent_step_uses_step_executor(
        self, mocker: MockerFixture
    ) -> None:
        """Agent step node calls StepExecutor.run() instead of run_step directly."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "done"},
        )

        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "test"},
            "spec": {"steps": [
                {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            ]},
        }

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        mock_executor.run.assert_called_once()
