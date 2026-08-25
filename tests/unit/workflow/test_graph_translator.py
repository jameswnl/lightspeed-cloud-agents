"""Tests for the YAML → pydantic-graph translation layer."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture


def _make_definition(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal workflow definition dict."""
    return {
        "apiVersion": "v1",
        "kind": "AgentWorkflow",
        "metadata": {"name": "test-workflow"},
        "spec": {"steps": steps},
    }


class TestGraphTranslator:
    """Tests for translating workflow YAML to pydantic-graph."""

    def test_single_agent_step(self) -> None:
        """Single agent step produces a graph with start → agent → end."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None
        assert state is not None

    def test_two_sequential_steps(self) -> None:
        """Two agent steps produce start → A → B → end."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix it",
                "output_key": "fix_result",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_approval_step_included(self) -> None:
        """Human-approval step translates to a graph node."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve the fix?",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_state_has_workflow_id(self) -> None:
        """Workflow state carries the workflow_id."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        _, state = build_graph(defn, workflow_id="wf-test-42")
        assert state.workflow_id == "wf-test-42"

    def test_state_has_step_definitions(self) -> None:
        """Workflow state carries the step definitions for runtime access."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check it",
                "output_key": "diagnosis",
            },
        ])

        _, state = build_graph(defn, workflow_id="wf-1")
        assert "diagnose" in state.step_defs

    @pytest.mark.asyncio
    async def test_agent_step_calls_step_runner(
        self, mocker: MockerFixture
    ) -> None:
        """Agent step node calls step_runner.run_step during execution."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "all good"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            sandbox_image="sandbox:latest",
        )

        result = await graph.run(state=state)
        mock_executor.run.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_auto_approve_completes(self, mocker: MockerFixture) -> None:
        """Approval step with auto_approve=True completes without pausing."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            approval_policy={"auto_approve": True},
        )

        result = await graph.run(state=state)
        assert result["status"] == "completed"
        assert state.step_results["approval"]["output"]["auto_approved"] is True
        assert state.paused_at_step is None

    @pytest.mark.asyncio
    async def test_approval_signals_pause(self, mocker: MockerFixture) -> None:
        """Approval step without auto_approve signals pause."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
        ])

        graph, state = build_graph(defn, workflow_id="wf-1")

        result = await graph.run(state=state)
        assert state.paused_at_step == "approve"
        assert state.step_results["approval"]["status"] == "awaiting_approval"

    @pytest.mark.asyncio
    async def test_step_results_keyed_by_output_key(
        self, mocker: MockerFixture
    ) -> None:
        """Agent step stores results under output_key, not step name."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"found": "bug"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check",
                "output_key": "my_diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert "my_diagnosis" in state.step_results
        assert state.step_results["my_diagnosis"]["output"]["found"] == "bug"

    @pytest.mark.asyncio
    async def test_condition_false_skips_step(self, mocker: MockerFixture) -> None:
        """Step with false condition is skipped."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.evaluate_condition",
            return_value=False,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
                "condition": "steps.diag.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        mock_executor.run.assert_not_called()
        assert state.step_results["r1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_condition_true_runs_step(self, mocker: MockerFixture) -> None:
        """Step with true condition runs normally."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.evaluate_condition",
            return_value=True,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
                "condition": "steps.diag.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.step_results["r1"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_pause_guard_skips_subsequent_steps(
        self, mocker: MockerFixture
    ) -> None:
        """Steps after approval gate are skipped when workflow is paused."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "approve",
                "type": "human-approval",
                "output_key": "approval",
                "message": "Approve?",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix it",
                "output_key": "fix_result",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.paused_at_step == "approve"
        mock_executor.run.assert_not_called()
        assert state.step_results["fix_result"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_condition_integration_unmocked(
        self, mocker: MockerFixture
    ) -> None:
        """Condition evaluation works end-to-end without mocking evaluate_condition."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"severity": "low"},
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Diagnose",
                "output_key": "diagnosis",
            },
            {
                "name": "fix",
                "type": "agent",
                "prompt": "Fix",
                "output_key": "fix_result",
                "condition": "steps.diagnosis.output.severity == 'high'",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.step_results["fix_result"]["status"] == "skipped"

    def test_parallel_group_warns(self, caplog: Any) -> None:
        """Step with parallel_group logs a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            from cloud_agents.workflow.executor.graph_translator import build_graph

            defn = _make_definition([
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                    "parallel_group": "group-a",
                },
            ])

            build_graph(defn, workflow_id="wf-1")

        assert any("parallel_group" in r.message for r in caplog.records)

    def test_no_temporal_imports(self) -> None:
        """graph_translator module has zero temporalio imports."""
        from cloud_agents.workflow.executor import graph_translator as mod

        source = open(mod.__file__).read()
        assert "from temporalio" not in source
        assert "import temporalio" not in source


class TestGraphTranslatorOtelTracing:
    """Tests for OTEL tracing in graph_translator agent_step."""

    @pytest.mark.asyncio
    async def test_span_created_with_correct_name(
        self, mocker: MockerFixture
    ) -> None:
        """agent_step creates a span named 'step.execute'."""
        from unittest.mock import MagicMock

        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
            input_tokens=100,
            output_tokens=50,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        # Mock the tracer
        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0x0AF7651916CD43DD8448EB211C80319C
        mock_span.get_span_context.return_value = mock_span_ctx
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span

        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator._tracer",
            mock_tracer,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-trace-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "step.execute"

    @pytest.mark.asyncio
    async def test_span_has_correct_attributes(
        self, mocker: MockerFixture
    ) -> None:
        """MiddlewareExecutor sets step.name and workflow.id on the span."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
            input_tokens=200,
            output_tokens=80,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check",
                "output_key": "diagnosis",
                "spawn": "ephemeral",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-trace-2",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        # MiddlewareExecutor opens span with step.name and workflow.id.
        # With the default NoOp tracer, span attributes are no-op.
        # Detailed span attribute assertions in test_step_middleware.py.
        result = await graph.run(state=state)
        assert result is not None
        assert state.step_results["diagnosis"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_span_records_result_attributes(
        self, mocker: MockerFixture
    ) -> None:
        """TracingMiddleware sets result attributes on span (tested in middleware unit tests)."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
            input_tokens=150,
            output_tokens=75,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        result = await graph.run(state=state)
        # Verify execution succeeded -- TracingMiddleware span attribute
        # assertions are in test_step_middleware.py::TestTracingMiddleware.
        assert result is not None
        assert state.step_results["r1"]["status"] == "completed"


class TestGraphTranslatorTraceIdPropagation:
    """Tests for trace_id propagation to StepMetadata."""

    @pytest.mark.asyncio
    async def test_trace_id_set_on_metadata(
        self, mocker: MockerFixture
    ) -> None:
        """TracingMiddleware is wired and metadata is populated.

        Trace ID propagation from OTEL span to StepMetadata is verified
        in test_step_middleware.py::TestTracingMiddleware. This test
        confirms that graph_translator's middleware stack is connected.
        """
        from cloud_agents.workflow.executor.step.base import StepResult

        captured_input: dict[str, Any] = {}

        async def capture_run(step_input: Any) -> StepResult:
            captured_input["metadata"] = step_input.metadata
            return StepResult(
                status="completed",
                output={"ok": True},
                input_tokens=10,
                output_tokens=5,
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = capture_run
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        # Metadata is populated (user_id/session_id from state)
        assert captured_input["metadata"] is not None
        # With NoOp tracer, trace_id stays None (no active span)
        assert captured_input["metadata"].trace_id is None

    @pytest.mark.asyncio
    async def test_trace_id_not_set_when_no_trace(
        self, mocker: MockerFixture
    ) -> None:
        """trace_id remains None when span has no trace context."""
        from unittest.mock import MagicMock

        from cloud_agents.workflow.executor.step.base import StepResult

        captured_input: dict[str, Any] = {}

        async def capture_run(step_input: Any) -> StepResult:
            captured_input["metadata"] = step_input.metadata
            return StepResult(
                status="completed",
                output={"ok": True},
                input_tokens=10,
                output_tokens=5,
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = capture_run
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        # Simulate NoOp tracer -- span_context.trace_id = 0
        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0
        mock_span.get_span_context.return_value = mock_span_ctx
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span

        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator._tracer",
            mock_tracer,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert captured_input["metadata"] is not None
        assert captured_input["metadata"].trace_id is None


class TestGraphTranslatorTranscriptEnrichment:
    """Tests for ConversationMessage transcript enrichment."""

    @pytest.mark.asyncio
    async def test_messages_saved_to_transcript_store(
        self, mocker: MockerFixture
    ) -> None:
        """TranscriptMiddleware saves ConversationMessages to transcript_store."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "all good"},
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "diagnose",
                "type": "agent",
                "prompt": "Check the cluster",
                "output_key": "diagnosis",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            transcript_store=mock_transcript_store,
        )

        await graph.run(state=state)

        mock_transcript_store.save.assert_called_once()
        call_kwargs = mock_transcript_store.save.call_args[1]
        assert call_kwargs["workflow_id"] == "wf-1"
        assert call_kwargs["step_name"] == "diagnosis"
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Check the cluster"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_trace_id_passed_to_transcript_store(
        self, mocker: MockerFixture
    ) -> None:
        """TranscriptMiddleware passes trace_id to transcript_store.save.

        With NoOp tracer (no OTEL endpoint), trace_id is None.
        Full trace_id propagation is tested in test_step_middleware.py.
        """
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
            input_tokens=10,
            output_tokens=5,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            transcript_store=mock_transcript_store,
        )

        await graph.run(state=state)

        call_kwargs = mock_transcript_store.save.call_args[1]
        # NoOp tracer means no trace_id propagated
        assert call_kwargs["trace_id"] is None

    @pytest.mark.asyncio
    async def test_no_save_when_no_transcript_store(
        self, mocker: MockerFixture
    ) -> None:
        """No error when transcript_store is None."""
        from unittest.mock import MagicMock

        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"ok": True},
            input_tokens=10,
            output_tokens=5,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "test",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            transcript_store=None,
        )

        # Should not raise
        await graph.run(state=state)

    @pytest.mark.asyncio
    async def test_assistant_content_is_json_output(
        self, mocker: MockerFixture
    ) -> None:
        """Assistant message content is JSON-serialized output."""
        import json

        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"severity": "high", "details": "OOM on pod-1"},
            input_tokens=10,
            output_tokens=5,
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {
                "name": "s1",
                "type": "agent",
                "prompt": "check",
                "output_key": "r1",
            },
        ])

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            transcript_store=mock_transcript_store,
        )

        await graph.run(state=state)

        call_kwargs = mock_transcript_store.save.call_args[1]
        messages = call_kwargs["messages"]
        assistant_msg = messages[1]
        parsed = json.loads(assistant_msg["content"])
        assert parsed["severity"] == "high"
        assert parsed["details"] == "OOM on pod-1"


class TestGraphTranslatorResumeTraceContinuity:
    """Tests for span-link trace continuity across pause/resume (issue #179)."""

    @pytest.mark.asyncio
    async def test_trace_parent_captured_after_step(
        self, mocker: MockerFixture
    ) -> None:
        """state.trace_parent picks up the step's captured traceparent.

        Actual traceparent capture (from a real OTEL span) is verified in
        test_step_middleware.py. This test confirms graph_translator wires
        the captured value from step_input.metadata.extra onto state.
        """
        from cloud_agents.workflow.executor.step.base import StepResult

        async def capture_run(step_input: Any) -> StepResult:
            step_input.metadata.extra["trace_parent"] = "00-aaaa-bbbb-01"
            return StepResult(
                status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = capture_run
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
        ])
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert state.trace_parent == "00-aaaa-bbbb-01"

    @pytest.mark.asyncio
    async def test_trace_parent_cleared_when_later_step_capture_fails(
        self, mocker: MockerFixture
    ) -> None:
        """A step with no captured trace_parent must not leave an earlier
        step's stale trace_parent in place -- a pause right after would
        otherwise link to the wrong pre-pause span.
        """
        from cloud_agents.workflow.executor.step.base import StepResult

        async def capture_then_fail(step_input: Any) -> StepResult:
            if step_input.step_name == "s1":
                step_input.metadata.extra["trace_parent"] = "00-aaaa-bbbb-01"
            # s2's capture "fails" (swallowed elsewhere) -- nothing set
            return StepResult(
                status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
            )

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = capture_then_fail
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
        ])
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert state.trace_parent is None

    @pytest.mark.asyncio
    async def test_resume_trace_parent_becomes_link_once(
        self, mocker: MockerFixture
    ) -> None:
        """Only the first step after resume gets a Link; then it's consumed."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mocker.AsyncMock(),
        )

        step_result = StepResult(
            status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
        )
        mock_instance = mocker.MagicMock()
        mock_instance.run = mocker.AsyncMock(return_value=step_result)
        mock_me_class = mocker.MagicMock(return_value=mock_instance)
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.MiddlewareExecutor",
            mock_me_class,
        )

        fake_traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
        ])
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )
        state.resume_trace_parent = fake_traceparent

        await graph.run(state=state)

        assert mock_me_class.call_count == 2
        first_links = mock_me_class.call_args_list[0].kwargs["links"]
        second_links = mock_me_class.call_args_list[1].kwargs["links"]

        assert first_links is not None
        assert len(first_links) == 1
        assert format(first_links[0].context.trace_id, "032x") == (
            "0af7651916cd43dd8448eb211c80319c"
        )

        assert second_links is None
        assert state.resume_trace_parent is None

    @pytest.mark.asyncio
    async def test_no_resume_trace_parent_means_no_links(
        self, mocker: MockerFixture
    ) -> None:
        """Normal (non-resumed) execution passes no links."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mocker.AsyncMock(),
        )

        step_result = StepResult(
            status="completed", output={"ok": True}, input_tokens=1, output_tokens=1
        )
        mock_instance = mocker.MagicMock()
        mock_instance.run = mocker.AsyncMock(return_value=step_result)
        mock_me_class = mocker.MagicMock(return_value=mock_instance)
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.MiddlewareExecutor",
            mock_me_class,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([
            {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
        ])
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert mock_me_class.call_args_list[0].kwargs["links"] is None
