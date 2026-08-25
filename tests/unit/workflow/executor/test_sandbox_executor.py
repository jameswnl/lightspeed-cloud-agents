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

    @pytest.mark.asyncio
    async def test_token_usage_extracted_from_result_event(self, mocker: MockerFixture) -> None:
        """StepResult carries real token/cost usage from the transcript's result event.

        Regression test for #188 bug 2: these were hardcoded to 0 even
        though the real numbers are present in the transcript's "result"
        event data (written by lightspeed_agentic.logging.EventLogger),
        just never read.
        """
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            return_value={
                "status": "completed",
                "output": {"summary": "done"},
                "transcript": {
                    "step_name": "s1",
                    "events": [
                        {"ts": "t1", "type": "tool_call", "data": {"name": "kubectl_get"}},
                        {
                            "ts": "t2",
                            "type": "result",
                            "data": {
                                "text": "done",
                                "cost_usd": 0.0042,
                                "input_tokens": 26,
                                "output_tokens": 136,
                            },
                        },
                    ],
                },
            },
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=mocker.AsyncMock())
        result = await executor.run(StepInput(
            prompt="Check the cluster",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            workflow_id="wf-1",
            step_name="diagnose",
            output_key="diagnosis",
        ))

        assert result.input_tokens == 26
        assert result.output_tokens == 136
        assert result.cost_usd == 0.0042

    @pytest.mark.asyncio
    async def test_token_usage_summed_across_multiple_result_events(
        self, mocker: MockerFixture
    ) -> None:
        """Multiple result events (multi-turn agent) have their usage summed."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            return_value={
                "status": "completed",
                "output": {"summary": "done"},
                "transcript": {
                    "step_name": "s1",
                    "events": [
                        {
                            "ts": "t1",
                            "type": "result",
                            "data": {"cost_usd": 0.01, "input_tokens": 10, "output_tokens": 5},
                        },
                        {
                            "ts": "t2",
                            "type": "result",
                            "data": {"cost_usd": 0.02, "input_tokens": 20, "output_tokens": 15},
                        },
                    ],
                },
            },
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=mocker.AsyncMock())
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.input_tokens == 30
        assert result.output_tokens == 20
        assert result.cost_usd == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_zero_usage_when_no_result_event(self, mocker: MockerFixture) -> None:
        """No result event (e.g. empty transcript) -> zero usage, not a crash."""
        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)
        mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            return_value={
                "status": "failed",
                "error": "agent returned success=false",
                "output": None,
                "transcript": {"step_name": "s1", "events": []},
            },
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=mocker.AsyncMock())
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_duration_ms_reflects_real_elapsed_time(self, mocker: MockerFixture) -> None:
        """duration_ms is measured around run_step(), not hardcoded to 0."""
        import asyncio

        mocker.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False)

        async def slow_run_step(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"status": "completed", "output": {}, "transcript": {"events": []}}

        mocker.patch(
            "cloud_agents.workflow.core.step_runner.run_step",
            side_effect=slow_run_step,
        )

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.sandbox import SandboxExecutor

        executor = SandboxExecutor(spawner=mocker.AsyncMock())
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.duration_ms >= 40
