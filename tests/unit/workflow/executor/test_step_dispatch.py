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

    def test_none_returns_direct_executor(self) -> None:
        """spawn: none returns DirectExecutor."""
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        executor = get_step_executor(
            step={"name": "s1", "spawn": "none"},
            spawner=None,
        )
        assert isinstance(executor, DirectExecutor)

    def test_local_returns_subprocess_executor(self) -> None:
        """spawn: local returns SubprocessExecutor."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        executor = get_step_executor(
            step={"name": "s1", "spawn": "local"},
            spawner=None,
        )
        assert isinstance(executor, SubprocessExecutor)

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


class TestSpawnModeEngineParity:
    """Both engines dispatch spawn: none/local through the same get_step_executor()
    call (issue #228) -- proving structural parity, not bit-identical LLM output.
    """

    def test_none_and_local_executors_never_reference_the_spawner(self) -> None:
        """A spawner passed in is never touched for spawn: none/local.

        Unlike SandboxExecutor (constructed with spawner=...), DirectExecutor
        and SubprocessExecutor are constructed with no arguments at all --
        get_step_executor() doesn't even pass the spawner through. This is
        the structural guarantee both the local engine (graph_translator.py)
        and the Temporal engine (activities.py, since issue #228) rely on:
        whichever caller invokes get_step_executor(), a real spawner object
        passed for an unrelated ephemeral step elsewhere can never leak into
        a none/local step's execution.
        """
        from cloud_agents.workflow.executor.step.direct import DirectExecutor
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        class TrackedSpawner:
            def __init__(self) -> None:
                self.touched = False

            def __getattr__(self, name: str) -> Any:
                self.touched = True
                raise AssertionError(f"spawner.{name} should never be accessed")

        spawner = TrackedSpawner()

        none_executor = get_step_executor(step={"name": "s1", "spawn": "none"}, spawner=spawner)
        local_executor = get_step_executor(step={"name": "s2", "spawn": "local"}, spawner=spawner)

        assert isinstance(none_executor, DirectExecutor)
        assert isinstance(local_executor, SubprocessExecutor)
        assert not spawner.touched
        assert not hasattr(none_executor, "spawner")
        assert not hasattr(local_executor, "spawner")
