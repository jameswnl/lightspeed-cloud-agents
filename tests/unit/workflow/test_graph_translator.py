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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check the cluster",
                    "output_key": "diagnosis",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None
        assert state is not None

    def test_two_sequential_steps(self) -> None:
        """Two agent steps produce start → A → B → end."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
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
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_approval_step_included(self) -> None:
        """Human-approval step translates to a graph node."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
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
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        assert graph is not None

    def test_state_has_workflow_id(self) -> None:
        """Workflow state carries the workflow_id."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

        _, state = build_graph(defn, workflow_id="wf-test-42")
        assert state.workflow_id == "wf-test-42"

    def test_state_has_step_definitions(self) -> None:
        """Workflow state carries the step definitions for runtime access."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check it",
                    "output_key": "diagnosis",
                },
            ]
        )

        _, state = build_graph(defn, workflow_id="wf-1")
        assert "diagnose" in state.step_defs

    @pytest.mark.asyncio
    async def test_agent_step_calls_step_runner(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check the cluster",
                    "output_key": "diagnosis",
                },
            ]
        )

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
    async def test_agent_step_interpolates_prompt(self, mocker: MockerFixture) -> None:
        """Agent step prompt is interpolated with a prior step's output."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = [
            StepResult(status="completed", output={"root_cause": "disk full"}),
            StepResult(status="completed", output={"applied": True}),
        ]
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "triage",
                    "type": "agent",
                    "prompt": "Diagnose",
                    "output_key": "triage_result",
                },
                {
                    "name": "remediate",
                    "type": "agent",
                    "prompt": "Apply the fix for: {{ steps.triage_result.output.root_cause }}",
                    "output_key": "remediate_result",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        await graph.run(state=state)

        remediate_input = mock_executor.run.call_args_list[1].args[0]
        assert "{{" not in remediate_input.prompt
        assert "disk full" in remediate_input.prompt

    @pytest.mark.asyncio
    async def test_agent_step_interpolates_instructions(self, mocker: MockerFixture) -> None:
        """Agent step instructions (system_prompt) are interpolated too."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.side_effect = [
            StepResult(status="completed", output={"host": "node-1"}),
            StepResult(status="completed", output={"applied": True}),
        ]
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "triage",
                    "type": "agent",
                    "prompt": "Diagnose",
                    "output_key": "triage_result",
                },
                {
                    "name": "remediate",
                    "type": "agent",
                    "prompt": "Apply the fix",
                    "instructions": "Target host: {{ steps.triage_result.output.host }}",
                    "output_key": "remediate_result",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        await graph.run(state=state)

        remediate_input = mock_executor.run.call_args_list[1].args[0]
        assert "{{" not in remediate_input.system_prompt
        assert "node-1" in remediate_input.system_prompt

    @pytest.mark.asyncio
    async def test_agent_step_interpolation_fails_open(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unresolvable template reference falls back to the raw string."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(status="completed", output={"ok": True})
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        raw_prompt = "Fix: {{ steps.missing_step.output.x }}"
        defn = _make_definition(
            [
                {
                    "name": "solo",
                    "type": "agent",
                    "prompt": raw_prompt,
                    "output_key": "solo_result",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        # Agent-prompt interpolation now runs inside the canonical
        # build_step_input helper (issue #268), so the fail-open debug
        # record comes from the execution module.
        with caplog.at_level("DEBUG", logger="cloud_agents.workflow.core.execution"):
            await graph.run(state=state)

        solo_input = mock_executor.run.call_args_list[0].args[0]
        assert solo_input.prompt == raw_prompt
        assert "Template interpolation failed" in caplog.text

    @pytest.mark.asyncio
    async def test_auto_approve_completes(self, mocker: MockerFixture) -> None:
        """Approval step with auto_approve=True completes without pausing."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                    "message": "Approve?",
                },
            ]
        )

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

        defn = _make_definition(
            [
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                    "message": "Approve?",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")

        result = await graph.run(state=state)
        assert state.paused_at_step == "approve"
        assert state.step_results["approval"]["status"] == "awaiting_approval"
        assert state.step_results["approval"]["output"] == {"message": "Approve?"}

    @pytest.mark.asyncio
    async def test_approval_message_interpolated(self, mocker: MockerFixture) -> None:
        """Approval message is interpolated with prior step outputs (#197)."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(status="completed", output={"host": "node-1"})
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "triage",
                    "type": "agent",
                    "prompt": "Diagnose",
                    "output_key": "triage_result",
                },
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                    "message": "Apply fix to {{ steps.triage_result.output.host }}?",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        await graph.run(state=state)

        # interpolate() wraps substituted values in <data>...</data> (shared
        # prompt-injection boundary with LLM-facing prompts, incl. Temporal's
        # approval notifications) -- asserting the exact string here, not
        # just substring/absence checks, so a change to that wrapping shows
        # up as a test failure in the human-facing approval message too.
        assert state.step_results["approval"]["output"] == {
            "message": 'Apply fix to <data>"node-1"</data>?'
        }

    @pytest.mark.asyncio
    async def test_approval_message_absent_is_noop(self, mocker: MockerFixture) -> None:
        """Approval step with no message field pauses with a None output (#197)."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        await graph.run(state=state)

        assert state.step_results["approval"]["output"] is None

    @pytest.mark.asyncio
    async def test_approval_message_interpolation_fails_open(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unresolvable reference in the approval message falls back to the
        raw string rather than crashing the step (#197)."""
        from cloud_agents.workflow.executor.graph_translator import build_graph

        raw_message = "Approve for {{ steps.missing_step.output.x }}?"
        defn = _make_definition(
            [
                {
                    "name": "approve",
                    "type": "human-approval",
                    "output_key": "approval",
                    "message": raw_message,
                },
            ]
        )

        graph, state = build_graph(defn, workflow_id="wf-1")
        with caplog.at_level("DEBUG", logger="cloud_agents.workflow.executor.graph_translator"):
            await graph.run(state=state)

        assert state.step_results["approval"]["output"] == {"message": raw_message}
        assert "Template interpolation failed" in caplog.text

    @pytest.mark.asyncio
    async def test_step_results_keyed_by_output_key(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check",
                    "output_key": "my_diagnosis",
                },
            ]
        )

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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                    "condition": "steps.diag.output.severity == 'high'",
                },
            ]
        )

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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                    "condition": "steps.diag.output.severity == 'high'",
                },
            ]
        )

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)
        assert state.step_results["r1"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_pause_guard_skips_subsequent_steps(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
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
            ]
        )

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
    async def test_condition_integration_unmocked(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
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
            ]
        )

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

            defn = _make_definition(
                [
                    {
                        "name": "s1",
                        "type": "agent",
                        "prompt": "test",
                        "output_key": "r1",
                        "parallel_group": "group-a",
                    },
                ]
            )

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
    async def test_span_created_with_correct_name(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check cluster",
                    "output_key": "diagnosis",
                },
            ]
        )

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
    async def test_span_has_correct_attributes(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check",
                    "output_key": "diagnosis",
                    "spawn": "ephemeral",
                },
            ]
        )

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
    async def test_span_records_result_attributes(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

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
    async def test_trace_id_set_on_metadata(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

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
    async def test_trace_id_not_set_when_no_trace(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

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
    async def test_messages_saved_to_transcript_store(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "diagnose",
                    "type": "agent",
                    "prompt": "Check the cluster",
                    "output_key": "diagnosis",
                },
            ]
        )

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
    async def test_trace_id_passed_to_transcript_store(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

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
    async def test_no_save_when_no_transcript_store(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "test",
                    "output_key": "r1",
                },
            ]
        )

        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
            transcript_store=None,
        )

        # Should not raise
        await graph.run(state=state)

    @pytest.mark.asyncio
    async def test_assistant_content_is_json_output(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {
                    "name": "s1",
                    "type": "agent",
                    "prompt": "check",
                    "output_key": "r1",
                },
            ]
        )

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
    async def test_trace_parent_captured_after_step(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            ]
        )
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

        defn = _make_definition(
            [
                {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
                {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
            ]
        )
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert state.trace_parent is None

    @pytest.mark.asyncio
    async def test_resume_trace_parent_becomes_link_once(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
                {"name": "s2", "type": "agent", "prompt": "test2", "output_key": "r2"},
            ]
        )
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
    async def test_no_resume_trace_parent_means_no_links(self, mocker: MockerFixture) -> None:
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

        defn = _make_definition(
            [
                {"name": "s1", "type": "agent", "prompt": "test", "output_key": "r1"},
            ]
        )
        graph, state = build_graph(
            defn,
            workflow_id="wf-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )

        await graph.run(state=state)

        assert mock_me_class.call_args_list[0].kwargs["links"] is None


class TestOneStepWorkflowExecution:
    """One-step workflows execute through the identical runner path (#268).

    A standalone agent invocation becomes a one-step workflow: a bare
    single step (no name/output_key) normalizes to the documented
    agent/result convention and runs through the same StepInput
    construction, middleware (tracing + transcript persistence), and
    result storage as a step embedded in a multi-step workflow.
    """

    @pytest.mark.asyncio
    async def test_bare_one_step_executes_with_convention_names(
        self, mocker: MockerFixture
    ) -> None:
        """A prompt-only step runs as agent/result with run-level provider."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"summary": "ok"}
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition([{"prompt": "Inspect the cluster"}])
        graph, state = build_graph(
            defn,
            workflow_id="wf-one-step",
            provider={
                "name": "openai",
                "model": "gpt-4o",
                "credentials_secret": "OPENAI_API_KEY",
            },
            sandbox_image="sandbox:default",
            transcript_store=mock_transcript_store,
        )
        await graph.run(state=state)

        solo_input = mock_executor.run.call_args_list[0].args[0]
        assert solo_input.step_name == "agent"
        assert solo_input.output_key == "result"
        assert solo_input.prompt == "Inspect the cluster"
        assert solo_input.provider["name"] == "openai"
        assert solo_input.provider["model"] == "gpt-4o"
        # Pre-#269 execution path: the secret reference flows so
        # ensure_credentials_env keeps working; values never do.
        assert solo_input.provider["credentials_secret"] == "OPENAI_API_KEY"
        assert solo_input.sandbox_image == "sandbox:default"
        # Same persistence path as multi-step: result stored under the
        # output key and transcript middleware invoked.
        assert state.step_results["result"]["status"] == "completed"
        mock_transcript_store.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_step_matches_embedded_step_input(
        self, mocker: MockerFixture
    ) -> None:
        """A one-step workflow and the same step embedded multi-step build
        equal executor inputs (minus identity/conditionals), through the
        same middleware and persistence path."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"ok": True}
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.executor.graph_translator import build_graph

        provider = {"name": "openai", "model": "gpt-4o"}
        catalog = [{"name": "catalog-a", "url": "https://a"}]
        first_step = {
            "name": "agent",
            "type": "agent",
            "prompt": "Inspect the cluster",
            "output_key": "result",
            "tools": ["kubectl_get"],
            "mcp_servers": ["catalog-a"],
            "allowed_skills": ["kubernetes"],
            "timeout_seconds": 120,
        }
        one_step_defn = _make_definition([dict(first_step)])
        multi_step_defn = _make_definition(
            [
                dict(first_step),
                {
                    "name": "second",
                    "type": "agent",
                    "prompt": "Summarize",
                    "output_key": "summary",
                    "condition": "steps.result.output.ok == true",
                },
            ]
        )

        graph_one, state_one = build_graph(
            one_step_defn,
            workflow_id="wf-1",
            provider=dict(provider),
            mcp_servers=[dict(catalog[0])],
            transcript_store=mock_transcript_store,
        )
        await graph_one.run(state=state_one)
        graph_multi, state_multi = build_graph(
            multi_step_defn,
            workflow_id="wf-1",
            provider=dict(provider),
            mcp_servers=[dict(catalog[0])],
            transcript_store=mock_transcript_store,
        )
        await graph_multi.run(state=state_multi)

        first_one = mock_executor.run.call_args_list[0].args[0]
        # call 0 = one-step run; calls 1,2 = multi-step run (condition
        # references prior output; mock output {"ok": True} satisfies it).
        first_multi = mock_executor.run.call_args_list[1].args[0]
        assert first_one.prompt == first_multi.prompt
        assert first_one.provider == first_multi.provider
        assert first_one.tools == first_multi.tools
        assert first_one.sandbox_image == first_multi.sandbox_image
        assert first_one.mcp_servers == first_multi.mcp_servers == catalog
        assert first_one.allowed_skills == first_multi.allowed_skills == [
            "kubernetes"
        ]
        assert first_one.timeout_seconds == first_multi.timeout_seconds == 120
        # Same persistence path: first-step result stored under the same
        # output key, transcript middleware invoked for every step.
        assert state_one.step_results["result"]["status"] == "completed"
        assert state_multi.step_results["result"]["status"] == "completed"
        assert mock_transcript_store.save.call_count == 3


class TestNoSecretValuesInSerializedArtifacts:
    """Serialization sweep for the credentials invariant (issue #268).

    Runtime credential *values* must not enter workflow definitions,
    normalized specs, executor inputs, run state, persisted transcripts,
    or logs. References (secret names / env var keys) may flow; values
    must not -- even when the value sits in the process environment and
    the run resolves credentials from it.
    """

    @pytest.mark.asyncio
    async def test_secret_value_absent_from_all_artifacts(
        self,
        mocker: MockerFixture,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A one-step run leaves the env secret value out of every dump."""
        import json
        import logging

        sentinel = "sk-sentinel-secret-value-999"
        monkeypatch.setenv("OPENAI_API_KEY", sentinel)

        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed",
            output={"summary": "ok"},
            transcript=[
                {"ts": "t", "type": "result", "data": {"text": "done"}},
            ],
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        mock_transcript_store = mocker.AsyncMock()

        from cloud_agents.workflow.core.execution import normalize_workflow_step
        from cloud_agents.workflow.executor.graph_translator import build_graph

        step = {
            "name": "agent",
            "type": "agent",
            "prompt": "Inspect the cluster",
            "output_key": "result",
        }
        defn = _make_definition([dict(step)])
        provider = {
            "name": "openai",
            "model": "gpt-4o",
            "credentials_secret": "OPENAI_API_KEY",
        }

        with caplog.at_level(logging.DEBUG):
            graph, state = build_graph(
                defn,
                workflow_id="wf-sweep",
                provider=dict(provider),
                transcript_store=mock_transcript_store,
            )
            await graph.run(state=state)

        solo_input = mock_executor.run.call_args_list[0].args[0]
        normalized, _ = normalize_workflow_step(dict(step))

        artifacts = {
            "definition": json.dumps(defn),
            "normalized_spec": normalized.model_dump_json(),
            "step_input": repr(solo_input.__dict__),
            "step_results": json.dumps(state.step_results),
            "logs": caplog.text,
        }
        assert mock_transcript_store.save.call_count == 1
        save_kwargs = mock_transcript_store.save.call_args[1]
        artifacts["transcript"] = save_kwargs["transcript"].model_dump_json()
        artifacts["messages"] = json.dumps(save_kwargs["messages"])

        for artifact_name, dumped in artifacts.items():
            assert sentinel not in dumped, (
                f"secret value leaked into {artifact_name}"
            )
        # The reference still flows where execution needs it.
        assert solo_input.provider["credentials_secret"] == "OPENAI_API_KEY"


class TestNormalizationFailureIsStepFailure:
    """Invalid steps fail the step, never crash the run (S4)."""

    @pytest.mark.asyncio
    async def test_secret_mcp_step_fails_without_dispatch(
        self, mocker: MockerFixture
    ) -> None:
        """A secret-bearing step records failure and never dispatches."""
        mock_executor = mocker.AsyncMock()
        mock_dispatch = mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )

        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = _make_definition(
            [
                {
                    "name": "agent",
                    "type": "agent",
                    "prompt": "Inspect the cluster",
                    "output_key": "result",
                    "mcp_servers": [
                        {"name": "x", "url": "https://tok:abc@internal/x"}
                    ],
                }
            ]
        )
        graph, state = build_graph(
            defn, workflow_id="wf-1", provider={"name": "openai", "model": "m"}
        )
        await graph.run(state=state)

        assert state.step_results["result"]["status"] == "failed"
        assert "credentialed" in state.step_results["result"]["error"]
        mock_dispatch.assert_not_called()
        mock_executor.run.assert_not_called()


class TestPrecedenceChainLocalRunner:
    """Definition-level defaults outrank run-level args on the local runner.

    The documented chain (issue #268) is service/run → workflow
    definition → step; run-level provider and sandbox_image act as
    defaults only when the definition does not specify them.
    """

    @staticmethod
    def _mock_executor(mocker: MockerFixture) -> mocker.AsyncMock:
        """Patch get_step_executor with a completing executor."""
        from cloud_agents.workflow.executor.step.base import StepResult

        mock_executor = mocker.AsyncMock()
        mock_executor.run.return_value = StepResult(
            status="completed", output={"ok": True}
        )
        mocker.patch(
            "cloud_agents.workflow.executor.graph_translator.get_step_executor",
            return_value=mock_executor,
        )
        return mock_executor

    def _definition(self, top_provider: dict | None, spec: dict | None) -> dict:
        defn: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "AgentWorkflow",
            "metadata": {"name": "precedence"},
            "spec": {"steps": [{"name": "s1", "type": "agent", "prompt": "p", "output_key": "r1"}]},
        }
        if spec:
            defn["spec"].update(spec)
        if top_provider:
            defn["provider"] = top_provider
        return defn

    @pytest.mark.asyncio
    async def test_definition_provider_outranks_run_provider(
        self, mocker: MockerFixture
    ) -> None:
        """Definition provider wins; run credentials do not bind cross-name."""
        mock_executor = self._mock_executor(mocker)
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = self._definition(
            top_provider={"name": "claude", "model": "claude-sonnet"}, spec=None
        )
        graph, state = build_graph(
            defn,
            workflow_id="wf-prec-1",
            provider={"name": "openai", "model": "gpt-4o", "credentials_secret": "k"},
        )
        await graph.run(state=state)

        solo_input = mock_executor.run.call_args_list[0].args[0]
        assert solo_input.provider["name"] == "claude"
        assert solo_input.provider["model"] == "claude-sonnet"
        assert "credentials_secret" not in solo_input.provider

    @pytest.mark.asyncio
    async def test_definition_sandbox_image_outranks_run_image(
        self, mocker: MockerFixture
    ) -> None:
        """Definition-level spawn_config image wins over the run image."""
        mock_executor = self._mock_executor(mocker)
        from cloud_agents.workflow.executor.graph_translator import build_graph

        defn = self._definition(
            top_provider=None, spec={"spawn_config": {"sandbox_image": "img-def"}}
        )
        graph, state = build_graph(
            defn, workflow_id="wf-prec-2", sandbox_image="run-image"
        )
        await graph.run(state=state)

        solo_input = mock_executor.run.call_args_list[0].args[0]
        assert solo_input.sandbox_image == "img-def"
