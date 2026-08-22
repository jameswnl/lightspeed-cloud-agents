"""Tests for SubprocessExecutor — spawn: local pydantic-ai agent in subprocess."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from pytest_mock import MockerFixture


class TestSubprocessExecutorInstantiation:
    """Tests for SubprocessExecutor creation."""

    def test_implements_step_executor(self) -> None:
        """SubprocessExecutor is a StepExecutor."""
        from cloud_agents.workflow.executor.step.base import StepExecutor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        assert issubclass(SubprocessExecutor, StepExecutor)

    def test_construction(self) -> None:
        """SubprocessExecutor can be instantiated with no arguments."""
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        executor = SubprocessExecutor()
        assert executor is not None

    def test_no_temporal_imports(self) -> None:
        """SubprocessExecutor has zero temporalio imports."""
        from cloud_agents.workflow.executor.step import subprocess_exec as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


class TestSubprocessExecutorRun:
    """Tests for SubprocessExecutor.run() — subprocess execution."""

    @pytest.mark.asyncio
    async def test_runs_and_returns_result(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor runs agent in subprocess and returns result."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mock_result = {
            "status": "completed",
            "output": {"finding": "disk full on /var"},
            "transcript": [{"type": "llm.call", "model": "gpt-4o"}],
            "input_tokens": 200,
            "output_tokens": 100,
        }

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            return_value=mock_result,
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="Investigate the disk usage",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
            tools=["kubectl_get", "read_logs"],
            workflow_id="wf-1",
            step_name="investigate",
            output_key="investigation",
        ))

        assert result.status == "completed"
        assert result.output == {"finding": "disk full on /var"}
        assert result.input_tokens == 200
        assert result.output_tokens == 100

    @pytest.mark.asyncio
    async def test_subprocess_failure_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor returns failed when subprocess errors."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=RuntimeError("Child process crashed"),
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="Investigate",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.status == "failed"
        assert "crashed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor returns failed on timeout."""
        import asyncio

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=asyncio.TimeoutError(),
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="Slow task",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            timeout_seconds=5,
        ))

        assert result.status == "failed"
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor fails when credentials are missing."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        env_copy = {k: v for k, v in os.environ.items() if "OPENAI" not in k and "ANTHROPIC" not in k}
        mocker.patch.dict(os.environ, env_copy, clear=True)

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=ValueError("API key not found"),
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "openai-api-key"},
        ))

        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_duration_tracked(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor tracks execution duration."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            return_value={
                "status": "completed",
                "output": {"ok": True},
                "transcript": [],
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="Quick check",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        ))

        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_context_passed_to_subprocess(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor passes prior step context to the subprocess."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        captured_input = {}

        async def mock_subprocess(step_input_dict, **kwargs):
            captured_input.update(step_input_dict)
            return {
                "status": "completed",
                "output": {"ok": True},
                "transcript": [],
                "input_tokens": 10,
                "output_tokens": 5,
            }

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=mock_subprocess,
        )

        executor = SubprocessExecutor()
        await executor.run(StepInput(
            prompt="Fix based on diagnosis",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            context={
                "diagnosis": {
                    "status": "completed",
                    "output": {"issue": "OOM"},
                },
            },
        ))

        assert "context" in captured_input
        assert captured_input["context"]["diagnosis"]["output"]["issue"] == "OOM"


class TestSubprocessExecutorDispatch:
    """Tests for step dispatch integration."""

    def test_dispatch_returns_subprocess_executor(self) -> None:
        """get_step_executor returns SubprocessExecutor for spawn: local."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        step = {"name": "investigate", "type": "agent", "spawn": "local"}
        executor = get_step_executor(step, spawner=None)
        assert isinstance(executor, SubprocessExecutor)

    def test_dispatch_no_spawner_needed(self) -> None:
        """spawn: local works without any spawner configured."""
        from cloud_agents.workflow.executor.step.dispatch import get_step_executor

        step = {"name": "investigate", "type": "agent", "spawn": "local"}
        executor = get_step_executor(step, spawner=None)
        assert executor is not None


class TestChildProcessPayload:
    """Tests for the child process payload serialization."""

    def test_step_input_to_dict(self) -> None:
        """StepInput is serializable to dict for subprocess transfer."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import _step_input_to_dict

        step_input = StepInput(
            prompt="Investigate",
            system_prompt="You are a K8s expert",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            tools=["kubectl_get"],
            context={"prior": {"status": "completed", "output": {"ok": True}}},
            timeout_seconds=120,
            workflow_id="wf-1",
            step_name="investigate",
            output_key="result",
        )

        payload = _step_input_to_dict(step_input)
        assert payload["prompt"] == "Investigate"
        assert payload["system_prompt"] == "You are a K8s expert"
        assert payload["tools"] == ["kubectl_get"]
        assert payload["timeout_seconds"] == 120

        roundtrip = json.dumps(payload)
        assert isinstance(roundtrip, str)


class TestSubprocessCancellation:
    """Tests for cancellation handling."""

    @pytest.mark.asyncio
    async def test_cancellation_returns_failed(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor propagates CancelledError after cleanup."""
        import asyncio

        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            side_effect=asyncio.CancelledError(),
        )

        executor = SubprocessExecutor()
        with pytest.raises(asyncio.CancelledError):
            await executor.run(StepInput(
                prompt="Long task",
                provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            ))


class TestSubprocessAntropicRejection:
    """Tests for native Anthropic provider rejection in subprocess."""

    @pytest.mark.asyncio
    async def test_anthropic_without_base_url_fails(self, mocker: MockerFixture) -> None:
        """SubprocessExecutor rejects native Anthropic provider."""
        from cloud_agents.workflow.executor.step.base import StepInput
        from cloud_agents.workflow.executor.step.subprocess_exec import SubprocessExecutor

        mocker.patch(
            "cloud_agents.workflow.executor.step.subprocess_exec._run_in_subprocess",
            return_value={
                "status": "failed",
                "output": None,
                "error": "Provider 'anthropic' uses a non-OpenAI-compatible API.",
                "transcript": [],
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )

        executor = SubprocessExecutor()
        result = await executor.run(StepInput(
            prompt="test",
            provider={"name": "anthropic", "model": "claude-sonnet-5"},
        ))

        assert result.status == "failed"
