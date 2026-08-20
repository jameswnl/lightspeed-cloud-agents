"""Tests for SandboxExecutor wrapping step_runner.run_step."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture


class TestSandboxExecutor:
    """Tests for the SandboxExecutor wrapper."""

    @pytest.mark.asyncio
    async def test_delegates_to_run_step(self, mocker: MockerFixture) -> None:
        """SandboxExecutor calls step_runner.run_step with correct args."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        mock_run_step = mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            return_value={
                "status": "completed",
                "output": {"summary": "done"},
                "transcript": {"step_name": "s1", "events": []},
            },
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        spawner = mocker.AsyncMock()
        executor = SandboxExecutor(spawner=spawner)

        result = await executor.run(StepInput(
            prompt="Check the cluster",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            workflow_id="wf-1",
            step_name="diagnose",
            output_key="diagnosis",
        ))

        assert result.status == "completed"
        assert result.output == {"summary": "done"}
        mock_run_step.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_spawner_raises(self) -> None:
        """SandboxExecutor without spawner raises ValueError."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=None)

        with pytest.raises(ValueError, match="spawner"):
            await executor.run(StepInput(
                prompt="test",
                provider={"name": "openai", "model": "gpt-4o"},
                step_name="s1",
            ))

    @pytest.mark.asyncio
    async def test_failed_step_maps_correctly(self, mocker: MockerFixture) -> None:
        """Failed step result maps to StepResult with error."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            return_value={
                "status": "failed",
                "error": "agent failed",
                "output": None,
            },
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=mocker.AsyncMock())
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.status == "failed"
        assert result.error == "agent failed"

    def test_implements_step_executor(self) -> None:
        """SandboxExecutor is a StepExecutor."""
        from cloud_agents.workflow.executor.step.base import StepExecutor
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        assert issubclass(SandboxExecutor, StepExecutor)
