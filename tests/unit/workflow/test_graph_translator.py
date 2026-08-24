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
        """agent_step sets step.name, workflow.id, model on the span."""
        from unittest.mock import MagicMock

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

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0x1234567890ABCDEF
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

        await graph.run(state=state)

        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        attrs = call_kwargs["attributes"]
        assert attrs["step.name"] == "diagnose"
        assert attrs["workflow.id"] == "wf-trace-2"
        assert attrs["model"] == "gpt-4o"
        assert attrs["step.spawn"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_span_records_result_attributes(
        self, mocker: MockerFixture
    ) -> None:
        """agent_step sets step.status, input_tokens, output_tokens on span after run."""
        from unittest.mock import MagicMock

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

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0xABCDEF
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

        set_attr_calls = mock_span.set_attribute.call_args_list
        attr_dict = {c[0][0]: c[0][1] for c in set_attr_calls}
        assert attr_dict["step.status"] == "completed"
        assert attr_dict["step.input_tokens"] == 150
        assert attr_dict["step.output_tokens"] == 75


class TestGraphTranslatorTraceIdPropagation:
    """Tests for trace_id propagation to StepMetadata."""

    @pytest.mark.asyncio
    async def test_trace_id_set_on_metadata(
        self, mocker: MockerFixture
    ) -> None:
        """trace_id from span context is set on step_input.metadata."""
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

        # trace_id = 0x0af7651916cd43dd8448eb211c80319c -> "0af7651916cd43dd8448eb211c80319c"
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
        assert captured_input["metadata"].trace_id == "0af7651916cd43dd8448eb211c80319c"

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
        """ConversationMessage list is saved to transcript_store after execution."""
        from unittest.mock import MagicMock

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

        # NoOp tracer
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
        assert messages[1]["metadata"]["input_tokens"] == 100
        assert messages[1]["metadata"]["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_messages_include_trace_id(
        self, mocker: MockerFixture
    ) -> None:
        """trace_id is passed to transcript_store.save when tracing is active."""
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

        mock_span = MagicMock()
        mock_span_ctx = MagicMock()
        mock_span_ctx.trace_id = 0xDEADBEEF
        mock_span.get_span_context.return_value = mock_span_ctx
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_span
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator._tracer",
            mock_tracer,
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
        assert call_kwargs["trace_id"] == "000000000000000000000000deadbeef"

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
        from unittest.mock import MagicMock

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
