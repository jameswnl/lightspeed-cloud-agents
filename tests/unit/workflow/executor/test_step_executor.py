"""Tests for StepExecutor ABC, StepInput, and StepResult."""

from __future__ import annotations

import pytest


class TestStepInput:
    """Tests for StepInput dataclass."""

    def test_defaults(self) -> None:
        """StepInput has sensible defaults."""
        from cloud_agents.workflow.executor.step.base import StepInput

        inp = StepInput(prompt="test", provider={"name": "openai", "model": "gpt-4o"})
        assert inp.prompt == "test"
        assert inp.system_prompt is None
        assert inp.output_schema is None
        assert inp.tools == []
        assert inp.context == {}
        assert inp.timeout_seconds == 600

    def test_full_construction(self) -> None:
        """StepInput with all fields."""
        from cloud_agents.workflow.executor.step.base import StepInput

        inp = StepInput(
            prompt="diagnose",
            system_prompt="You are a K8s expert",
            output_schema={"type": "object", "properties": {"severity": {"type": "string"}}},
            tools=["kubectl_get", "read_logs"],
            context={"prior": {"status": "completed", "output": {"ok": True}}},
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            timeout_seconds=120,
            sandbox_image="sandbox:latest",
            skills_image="skills:latest",
            workflow_id="wf-1",
            step_name="diagnose",
        )
        assert inp.tools == ["kubectl_get", "read_logs"]
        assert inp.timeout_seconds == 120
        assert inp.workflow_id == "wf-1"


class TestStepResult:
    """Tests for StepResult dataclass."""

    def test_defaults(self) -> None:
        """StepResult has sensible defaults."""
        from cloud_agents.workflow.executor.step.base import StepResult

        result = StepResult(status="completed", output={"summary": "done"})
        assert result.status == "completed"
        assert result.transcript == []
        assert result.cost_usd == 0.0
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.duration_ms == 0

    def test_failed_result(self) -> None:
        """StepResult for a failed step."""
        from cloud_agents.workflow.executor.step.base import StepResult

        result = StepResult(
            status="failed",
            output=None,
            error="agent returned success=false",
        )
        assert result.status == "failed"
        assert result.error == "agent returned success=false"


class TestStepExecutorABC:
    """Tests for StepExecutor abstract base class."""

    def test_cannot_instantiate(self) -> None:
        """ABC prevents direct instantiation."""
        from cloud_agents.workflow.executor.step.base import StepExecutor

        with pytest.raises(TypeError):
            StepExecutor()  # type: ignore[abstract]

    def test_requires_run_method(self) -> None:
        """Subclass without run() cannot be instantiated."""
        from cloud_agents.workflow.executor.step.base import StepExecutor

        class Incomplete(StepExecutor):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_concrete_subclass_works(self) -> None:
        """Concrete subclass can be instantiated and called."""
        from cloud_agents.workflow.executor.step.base import (
            StepExecutor,
            StepInput,
            StepResult,
        )

        class DummyExecutor(StepExecutor):
            async def run(self, step_input: StepInput) -> StepResult:
                return StepResult(status="completed", output={"echo": step_input.prompt})

        executor = DummyExecutor()
        result = await executor.run(
            StepInput(prompt="hello", provider={"name": "openai", "model": "gpt-4o"})
        )
        assert result.status == "completed"
        assert result.output == {"echo": "hello"}

    def test_no_temporal_imports(self) -> None:
        """Step executor base has zero temporalio imports."""
        from cloud_agents.workflow.executor.step import base as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


class TestStepExecutorRunStream:
    """Tests for StepExecutor.run_stream() default implementation."""

    @pytest.mark.asyncio
    async def test_default_run_stream_yields_complete_event(self) -> None:
        """Default run_stream() wraps run() and yields a single complete event."""
        from cloud_agents.workflow.executor.step.base import (
            StepExecutor,
            StepInput,
            StepResult,
            StreamEvent,
        )

        class DummyExecutor(StepExecutor):
            async def run(self, step_input: StepInput) -> StepResult:
                return StepResult(
                    status="completed",
                    output={"echo": step_input.prompt},
                    input_tokens=10,
                    output_tokens=5,
                    duration_ms=42,
                )

        executor = DummyExecutor()
        step_input = StepInput(
            prompt="hello",
            provider={"name": "openai", "model": "gpt-4o"},
        )

        events: list[StreamEvent] = []
        async for event in executor.run_stream(step_input):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "complete"
        assert events[0].result is not None
        assert events[0].result.status == "completed"
        assert events[0].result.output == {"echo": "hello"}

    @pytest.mark.asyncio
    async def test_default_run_stream_preserves_step_result_fields(self) -> None:
        """Default run_stream() preserves all StepResult fields from run()."""
        from cloud_agents.workflow.executor.step.base import (
            StepExecutor,
            StepInput,
            StepResult,
        )

        class MetricsExecutor(StepExecutor):
            async def run(self, step_input: StepInput) -> StepResult:
                return StepResult(
                    status="completed",
                    output={"ok": True},
                    input_tokens=100,
                    output_tokens=50,
                    duration_ms=500,
                )

        executor = MetricsExecutor()
        step_input = StepInput(
            prompt="test",
            provider={"name": "openai", "model": "gpt-4o"},
        )

        events = [event async for event in executor.run_stream(step_input)]

        assert len(events) == 1
        result = events[0].result
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.duration_ms == 500
